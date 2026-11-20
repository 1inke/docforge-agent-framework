"""Core Data Contracts and Working Memory Models for DocForge Agent Framework (DAF §3.2, §3.3, §6 & Remediation R1/R2)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_hash_dict(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class StrictContractModel(BaseModel):
    """Base contract model enforcing strict schema validation (extra='forbid')."""

    model_config = ConfigDict(extra="forbid")


class ProjectSourcePolicy(StrictContractModel):
    """None allows ingested project sources; [] authorizes no source."""

    policy_version: Literal["1.0"] = "1.0"
    revision: int = Field(default=1, ge=1)
    mode: Literal["project_only", "approved_external", "open_research"] = "project_only"
    allowed_source_ids: list[str] | None = None
    approved_external_source_ids: list[str] = Field(default_factory=list)
    approved_external_domains: list[str] = Field(default_factory=list)
    require_evidence_registration: Literal[True] = True
    external_lookup_enabled: bool = False
    updated_by: str = "trusted_initialization"
    reason: str = "Initial project-only policy."
    approved_at: str | None = None


class GuidanceInstruction(StrictContractModel):
    id: str
    level: Literal["advisory", "contract", "enforced"]
    text: str


class TaskGuidanceContract(StrictContractModel):
    schema_version: Literal["guidance.v1"] = "guidance.v1"
    project_id: str
    task_kind: Literal[
        "source_analysis", "evidence_extraction", "plan_outline", "draft_section",
        "revise_section", "review_section", "global_review", "release_preflight",
    ]
    section_id: str | None = None
    project_stage: str
    profile_id: str
    state_version: int
    policy_revision: int
    template_version: str
    guidance_hash: str = ""
    validity: dict[str, Any]
    readiness: Literal["ready", "blocked", "needs_input", "stale"]
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    task_status: Literal["not_started", "in_progress", "completed", "blocked", "stale"] = "not_started"
    next_action: dict[str, Any] = Field(default_factory=dict)
    goal: str
    source_scope: dict[str, Any]
    profile_rules: dict[str, Any]
    decision_constraints: dict[str, Any]
    recommended_tools: list[str]
    required_inputs: list[str]
    instructions: list[GuidanceInstruction]
    method_guidance: list[str]
    response_rules: list[str]
    output_contract: dict[str, Any]
    exit_criteria: list[str]
    verification_steps: list[str]
    failure_actions: list[dict[str, str]]
    instruction_text: str


# ============================================================================
# 1. Document Profile Contract (3 Layers: Content + Structure + Layout, §6.1)
# ============================================================================


class ContentProfile(StrictContractModel):
    discourse_style: str = "academic_rigorous_argumentation"
    evidence_density: Literal["high", "medium", "low"] = "high"
    tone_constraints: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    terminology_consistency_required: bool = True


class SectionRule(StrictContractModel):
    section_id: str
    title: str
    required: bool = True
    min_words: int = 100
    max_words: int = 4000
    depends_on: list[str] = Field(default_factory=list)
    required_elements: list[str] = Field(default_factory=list)
    review_checklist: list[str] = Field(default_factory=list)


class StructureProfile(StrictContractModel):
    required_sections: list[SectionRule] = Field(default_factory=list)
    optional_sections: list[SectionRule] = Field(default_factory=list)
    allow_custom_sections: bool = True


class LayoutProfile(StrictContractModel):
    cjk_font: str = "宋体"
    heading_cjk_font: str = "黑体"
    latin_font: str = "Times New Roman"
    body_font_size_pt: float = 12.0
    line_spacing: float = 1.5
    first_line_indent_chars: float = 2.0
    equation_mode: Literal["pandoc_omml", "plain_text_fallback"] = "pandoc_omml"
    table_caption_style: Literal["above_table", "below_table"] = "above_table"
    figure_caption_style: Literal["below_figure", "above_figure"] = "below_figure"


class DocumentProfileContract(StrictContractModel):
    """Three-layer Document Profile Contract (§6.1)."""

    profile_id: str
    display_name: str
    description: str
    version: str = "1.0"
    content_profile: ContentProfile
    structure_profile: StructureProfile
    layout_profile: LayoutProfile
    approval_policy: list[Literal["outline", "section", "final_draft"]] = Field(
        default_factory=lambda: ["outline", "final_draft"]
    )
    block_release_on_major_issues: bool = True


# ============================================================================
# 2. Source Registry & Versioned 3-Layer Evidence Ledger (DAF-P1-05 & R2-P0-03)
# ============================================================================


class SourceBlockSlice(StrictContractModel):
    slice_id: str
    source_id: str
    locator: str
    raw_text: str
    normalized_text: str
    char_count: int
    sha256: str


class SourceDocumentRecord(StrictContractModel):
    source_id: str
    file_name: str
    file_path: str
    file_sha256: str
    file_type: str
    ingested_at: str = Field(default_factory=utc_now_iso)
    slice_ids: list[str] = Field(default_factory=list)
    is_agent_generated_summary: bool = False


class TemplateInspectionReport(StrictContractModel):
    template_path: str
    template_sha256: str
    template_mode: Literal["builtin", "tagged", "mapped"]
    detected_placeholders: list[str] = Field(default_factory=list)
    heading_Anchors: list[str] = Field(default_factory=list)
    writable_table_cells: list[str] = Field(default_factory=list)
    style_names: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SourceRegistry(StrictContractModel):
    documents: dict[str, SourceDocumentRecord] = Field(default_factory=dict)
    slices: dict[str, SourceBlockSlice] = Field(default_factory=dict)
    template_inspection: TemplateInspectionReport | None = None


class EvidenceRevision(StrictContractModel):
    """Auditable historical revision of an EvidenceItem (R2-P0-03 §3.2)."""

    evidence_id: str
    revision: int
    fingerprint: str
    source_id: str
    slice_id: str
    source_file_sha256: str = ""
    quote_hash: str = ""
    claim_summary: str = ""
    quote_excerpt: str = ""
    excerpt_match_status: str = "matched"
    source_reliability: str = "trusted"
    changed_fields: list[str] = Field(default_factory=list)
    archived_at: str = Field(default_factory=utc_now_iso)

    @property
    def evidence_revision(self) -> int:
        return self.revision


class EvidenceItem(StrictContractModel):
    """Versioned Three-layer Evidence Record (DAF-P1-05 & R2-P0-03):
    - L1 excerpt_match_status: deterministic substring verification against immutable SourceBlockSlice.
    - L3 source_reliability: trustworthiness assessment of the underlying source document.
    - evidence_revision & evidence_fingerprint: versioned identity for cascade invalidation.
    """

    evidence_id: str
    source_id: str
    slice_id: str
    locator: str
    claim_summary: str
    quote_excerpt: str
    excerpt_match_status: Literal["matched", "not_matched", "unmatched", "source_stale", "stale"] = "matched"
    verification_status: Literal["verified", "unverified_excerpt"] = "verified"
    source_reliability: Literal[
        "trusted",
        "conditional",
        "unassessed",
        "rejected",
        "agent_generated_untrusted",
    ] = "trusted"
    source_file_sha256: str = ""
    quote_hash: str = ""
    evidence_revision: int = 1
    evidence_fingerprint: str = ""
    updated_at: str = Field(default_factory=utc_now_iso)
    updated_reason: str = ""
    is_agent_generated_summary: bool = False
    verification_note: str = ""
    metrics_data: dict[str, Any] = Field(default_factory=dict)
    applicable_sections: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sync_l1_l3_and_fingerprint(self) -> "EvidenceItem":
        if self.excerpt_match_status in {"not_matched", "unmatched", "source_stale", "stale"}:
            self.verification_status = "unverified_excerpt"
        elif self.verification_status == "unverified_excerpt":
            self.excerpt_match_status = "not_matched"
        else:
            self.verification_status = "verified"
            self.excerpt_match_status = "matched"

        # Prevent circular self-citation (§5.4.5): agent-generated summaries cannot be 'trusted' primary sources
        if self.is_agent_generated_summary and self.source_reliability == "trusted":
            self.source_reliability = "conditional"

        if not self.quote_hash and self.quote_excerpt:
            self.quote_hash = f"sha256:{hashlib.sha256(self.quote_excerpt.strip().encode('utf-8')).hexdigest()}"

        fp_digest = _canonical_hash_dict(
            {
                "source_id": self.source_id,
                "slice_id": self.slice_id,
                "source_file_sha256": self.source_file_sha256,
                "quote_hash": self.quote_hash,
                "claim_summary": self.claim_summary,
                "excerpt_match_status": self.excerpt_match_status,
                "source_reliability": self.source_reliability,
                "metrics_data": self.metrics_data,
            }
        )
        self.evidence_fingerprint = f"sha256:{fp_digest}"
        return self


class EvidenceLedger(StrictContractModel):
    items: dict[str, EvidenceItem] = Field(default_factory=dict)
    revision_history: dict[str, list[EvidenceRevision]] = Field(default_factory=dict)


# ============================================================================
# 3. Outline Graph, Terminology & Decision Log (§3.3)
# ============================================================================


class OutlineNode(StrictContractModel):
    section_id: str
    title: str
    depends_on: list[str] = Field(default_factory=list)
    writing_objectives: list[str] = Field(default_factory=list)
    key_claims: list[str] = Field(default_factory=list)
    assigned_evidence_ids: list[str] = Field(default_factory=list)
    target_min_words: int | None = None
    target_max_words: int | None = None


class OutlineGraph(StrictContractModel):
    outline_revision: int = 0
    outline_hash: str = ""
    global_thesis: str = ""
    nodes: list[OutlineNode] = Field(default_factory=list)
    word_budget_origins: dict[str, str] = Field(default_factory=dict)


class TerminologyRegistry(StrictContractModel):
    terms: dict[str, str] = Field(default_factory=dict)
    symbol_table: dict[str, str] = Field(default_factory=dict)


class DecisionRecord(StrictContractModel):
    decision_id: str
    timestamp: str = Field(default_factory=utc_now_iso)
    source: Literal["user_feedback", "approval_gate", "agent_proposal"]
    summary: str
    scope: list[str] = Field(default_factory=list)
    forbidden_modification_scopes: list[str] = Field(default_factory=list)


class DecisionLog(StrictContractModel):
    entries: list[DecisionRecord] = Field(default_factory=list)
    active_user_constraints: list[str] = Field(default_factory=list)
    forbidden_modification_scopes: list[str] = Field(default_factory=list)
    acknowledged_template_conflicts: list[str] = Field(default_factory=list)


# ============================================================================
# 4. Section Context (DAF-P0-01), L2a/L2b Claim Bindings (R2-P1-06) & Review (R2-P0-02)
# ============================================================================


class DependencySnapshot(StrictContractModel):
    """Deterministic task-scoped dependency snapshot for a section (DAF-P0-01)."""

    outline_revision: int = 0
    outline_hash: str = ""
    profile_version_hash: str = ""
    section_contract_hash: str = ""
    upstream_sections: dict[str, str] = Field(default_factory=dict)
    evidence: dict[str, str] = Field(default_factory=dict)
    terminology_hash: str = ""
    decision_constraints_hash: str = ""
    review_issues_hash: str = ""


class RuleCheckResult(StrictContractModel):
    """L2a deterministic rule check result on a SourceBinding (R2-P1-06 §6.3)."""

    binding_id: str
    status: Literal["rule_passed", "rule_failed", "needs_manual_check"]
    detected_conflicts: list[str] = Field(default_factory=list)
    rationale: str = ""


class SemanticSupportReview(StrictContractModel):
    """L2b semantic support review record bound to specific section & evidence hashes (R2-P1-06 §7.1)."""

    binding_id: str
    semantic_support_status: Literal[
        "pending",
        "supported_reviewed",
        "partially_supported",
        "unsupported",
        "hypothesis_proposal",
        "stale",
    ]
    reviewer: str
    review_hash: str
    section_hash: str
    evidence_hash: str
    evidence_revision: int = 1
    rationale: str = ""
    reviewed_at: str = Field(default_factory=utc_now_iso)


class SourceBinding(StrictContractModel):
    """L2a + L2b Claim–Evidence Binding Record (DAF-P1-05 & R2-P1-06):
    - rule_check_status (L2a): deterministic checks (quantitative conflict, overclaim, ungrounded metrics).
    - semantic_support_status (L2b): Agent/human semantic entailment judgment bound to section & evidence versions.
    """

    binding_id: str = ""
    section_id: str = ""
    section_revision: int = 0
    claim_text: str
    claim_type: Literal[
        "background_fact",
        "research_hypothesis",
        "experimental_result",
        "methodological_choice",
    ] = "experimental_result"
    evidence_id: str = ""
    evidence_quote_hash: str = ""
    rule_check_status: Literal["rule_passed", "rule_failed", "needs_manual_check"] = "rule_passed"
    semantic_support_status: Literal[
        "pending",
        "supported_reviewed",
        "partially_supported",
        "unsupported",
        "hypothesis_proposal",
        "stale",
    ] = "pending"
    support_status: Literal[
        "supported",
        "supported_reviewed",
        "partially_supported",
        "unsupported",
        "pending",
        "hypothesis_proposal",
        "stale",
    ] = "pending"
    support_rationale: str = ""
    rationale: str = ""
    reviewer: str = ""
    reviewed_against_revision: int | None = None
    reviewed_against_section_hash: str = ""
    reviewed_against_evidence_fingerprint: str = ""
    reviewed_evidence_revision: int | None = None

    @model_validator(mode="after")
    def _sync_rationale_and_status(self) -> "SourceBinding":
        if self.rationale and not self.support_rationale:
            self.support_rationale = self.rationale
        elif self.support_rationale and not self.rationale:
            self.rationale = self.support_rationale

        # If rule_check_status failed, semantic_support_status cannot override it to supported_reviewed
        if self.rule_check_status == "rule_failed":
            self.support_status = "unsupported"
        elif self.semantic_support_status == "supported_reviewed":
            self.support_status = "supported"
        elif self.semantic_support_status in {
            "partially_supported",
            "unsupported",
            "pending",
            "hypothesis_proposal",
            "stale",
        }:
            self.support_status = self.semantic_support_status
        return self


class StructuredBlock(StrictContractModel):
    block_id: str
    block_type: Literal["equation", "table", "figure_placeholder"]
    caption: str = ""
    latex: str = ""
    table_headers: list[str] = Field(default_factory=list)
    table_rows: list[list[str]] = Field(default_factory=list)


class ReviewIssue(StrictContractModel):
    """Structured review issue (§6.3)."""

    issue_id: str
    severity: Literal["blocker", "major", "minor"]
    category: Literal[
        "structure_completeness",
        "word_budget",
        "forbidden_phrase",
        "evidence_binding",
        "claim_support",
        "terminology_consistency",
        "cross_section_consistency",
        "format_equation",
        "semantic_argumentation",
        "logic_consistency",
        "user_feedback",
    ]
    target_section_id: str | None = None
    description: str
    suggested_fix: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["open", "resolved"] = "open"
    resolved_in_revision: int | None = None


class SemanticReviewFinding(StrictContractModel):
    """Individual finding reported in a SemanticReviewReport (R2-P0-02 §2.4)."""

    severity: Literal["blocker", "major", "minor"]
    category: Literal[
        "structure_completeness",
        "word_budget",
        "forbidden_phrase",
        "evidence_binding",
        "claim_support",
        "terminology_consistency",
        "cross_section_consistency",
        "format_equation",
        "semantic_argumentation",
        "logic_consistency",
        "user_feedback",
    ] = "semantic_argumentation"
    description: str
    suggested_fix: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class ChecklistItemSpec(StrictContractModel):
    """Numbered, verifiable checklist item in a SemanticReviewTask (R3-01 §5.2)."""

    check_id: str
    question: str
    required: bool = True


class ChecklistItemResult(StrictContractModel):
    """Structured per-item checklist answer in a SemanticReviewReport (R3-01 §5.2)."""

    check_id: str
    question: str = ""
    verdict: Literal["pass", "fail", "needs_revision", "not_applicable"]
    rationale: str
    section_locations: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class SemanticReviewReport(StrictContractModel):
    """Formal semantic review artifact submitted by an external Agent (R2-P0-02 §2.4, §7.1 & R3-01 §5.2)."""

    task_id: str
    project_id: str
    section_id: str
    section_revision: int
    section_content_hash: str
    context_fingerprint: str
    reviewer_agent: str
    review_summary: str
    # Historical missing decisions remain unknown; new submissions are validated at ingress.
    recommendation: Literal["approve", "revise", "block"] | None = None
    findings: list[SemanticReviewFinding] = Field(default_factory=list)
    claim_evaluations: list[dict[str, Any]] = Field(default_factory=list)
    checklist_hash: str = ""
    checklist_results: list[ChecklistItemResult] = Field(default_factory=list)
    checklist_verification_mode: Literal["structured_verified"] = "structured_verified"
    submitted_at: str = Field(default_factory=utc_now_iso)
    accepted_at: str | None = None
    policy_version: str = "v1"
    status: Literal["submitted", "accepted", "rejected", "stale"] = "submitted"


class SemanticReviewTask(StrictContractModel):
    """Semantic review task lifecycle: pending -> claimed -> submitted -> accepted (or expired/rejected/stale) (R2-P0-02 §2.3 & R3-01 §5.2)."""

    task_id: str
    # Missing in historical snapshots: retain readability without upgrading old reports.
    requires_structured_checklist: bool = False
    section_id: str = ""
    target_section_ids: list[str] = Field(default_factory=list)
    target_section_revision: int = 0
    target_section_content_hash: str = ""
    target_context_fingerprint: str = ""
    review_dimension: str
    checklist_questions: list[str] = Field(default_factory=list)
    checklist_items: list[ChecklistItemSpec] = Field(default_factory=list)
    checklist_hash: str = ""
    evaluation_rubric: str
    status: Literal[
        "pending",
        "claimed",
        "submitted",
        "accepted",
        "expired",
        "rejected",
        "stale",
    ] = "pending"
    claimed_by: str | None = None
    claimed_at: str | None = None
    submitted_at: str | None = None
    completed_at: str | None = None
    rejection_reason: str = ""
    stale_reason: str = ""
    report: SemanticReviewReport | None = None

    @model_validator(mode="after")
    def _sync_checklist_items_and_hash(self) -> "SemanticReviewTask":
        if self.checklist_questions and not self.checklist_items:
            self.checklist_items = [
                ChecklistItemSpec(
                    check_id=f"{self.task_id}:q{idx:02d}",
                    question=q,
                    required=True,
                )
                for idx, q in enumerate(self.checklist_questions, start=1)
            ]
        elif self.checklist_items and not self.checklist_questions:
            self.checklist_questions = [item.question for item in self.checklist_items]

        if self.checklist_items and not self.checklist_hash:
            digest = _canonical_hash_dict(
                {
                    "items": [
                        {"check_id": item.check_id, "question": item.question}
                        for item in self.checklist_items
                    ]
                }
            )
            self.checklist_hash = f"sha256:{digest}"
        return self


class ReviewLedger(StrictContractModel):
    issues: list[ReviewIssue] = Field(default_factory=list)
    semantic_review_tasks: list[SemanticReviewTask] = Field(default_factory=list)
    semantic_reports: dict[str, SemanticReviewReport] = Field(default_factory=dict)
    last_reviewed_at: str | None = None
    deterministic_checked_revisions: dict[str, int] = Field(default_factory=dict)
    reviewed_section_revisions: dict[str, int] = Field(default_factory=dict)


class UpstreamSectionDependency(StrictContractModel):
    section_id: str
    title: str
    revision: int
    content_hash: str = ""
    summary: str
    key_claims: list[str] = Field(default_factory=list)


class ContextBudgetMetadata(StrictContractModel):
    """Explicit character/budget measurement metadata for project and section context responses (R3-05 §4.2)."""

    unit: Literal["unicode_chars", "approx_chars"] = "unicode_chars"
    requested_limit: int
    measured_size: int
    count_scope: str = "serialized_response_utf8_decoded_chars"
    is_exact_token_count: bool = False
    deprecated_token_budget_used: bool = False
    conversion_formula: str = ""
    truncated_fields: list[str] = Field(default_factory=list)
    has_more: bool = False


class SectionContextContract(StrictContractModel):
    """Budgeted, boundary-marked context returned by get_section_context (§6.2, DAF-P0-01 & R3-03/05)."""

    project_id: str
    section_id: str
    action: Literal["draft", "revise", "review"]
    context_revision: str
    context_fingerprint: str = ""
    dependency_snapshot: DependencySnapshot = Field(default_factory=DependencySnapshot)
    current_section_revision: int
    untrusted_source_notice: str
    section_title: str
    section_objectives: list[str] = Field(default_factory=list)
    word_budget: dict[str, int]
    word_budget_source: dict[str, Any] = Field(default_factory=dict)
    discourse_constraints: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    terminology_glossary: dict[str, str] = Field(default_factory=dict)
    upstream_dependencies: list[UpstreamSectionDependency] = Field(default_factory=list)
    verified_evidence: list[EvidenceItem] = Field(default_factory=list)
    current_draft_markdown: str | None = None
    open_review_issues: list[ReviewIssue] = Field(default_factory=list)
    user_constraints: list[str] = Field(default_factory=list)
    forbidden_modification_scopes: list[str] = Field(default_factory=list)
    truncated: bool = False
    token_budget: int = 6000
    budget: ContextBudgetMetadata | None = None


class SectionSubmissionContract(StrictContractModel):
    """Optimistic-locked, context-verified, idempotent section submission payload (§6.3 & DAF-P0-01/P0-02)."""

    project_id: str
    section_id: str
    base_revision: int
    context_revision: str
    context_fingerprint: str = ""
    draft_mode: Literal["formal", "draft_preapproval"] = "formal"
    idempotency_key: str
    content_markdown: str
    summary: str
    claims: list[str] = Field(default_factory=list)
    source_bindings: list[SourceBinding] = Field(default_factory=list)
    structured_blocks: list[StructuredBlock] = Field(default_factory=list)
    resolved_issue_ids: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)


class SectionRevisionRecord(StrictContractModel):
    revision: int
    base_revision: int
    context_revision: str
    context_fingerprint: str = ""
    draft_mode: Literal["formal", "draft_preapproval"] = "formal"
    idempotency_key: str
    content_hash: str
    content_markdown: str
    summary: str
    claims: list[str] = Field(default_factory=list)
    source_bindings: list[SourceBinding] = Field(default_factory=list)
    structured_blocks: list[StructuredBlock] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    diff_from_previous: str = ""
    created_at: str = Field(default_factory=utc_now_iso)


class SectionState(StrictContractModel):
    section_id: str
    title: str
    revision: int = 0
    content_hash: str = ""
    context_revision_used: str = ""
    context_fingerprint_used: str = ""
    is_preapproval_draft: bool = False
    content_markdown: str = ""
    summary: str = ""
    claims: list[str] = Field(default_factory=list)
    source_bindings: list[SourceBinding] = Field(default_factory=list)
    structured_blocks: list[StructuredBlock] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    needs_revalidation: bool = False
    stale_reason: str = ""
    history: list[SectionRevisionRecord] = Field(default_factory=list)


class SectionVersions(StrictContractModel):
    sections: dict[str, SectionState] = Field(default_factory=dict)


# ============================================================================
# 5. Document IR (DAF-P1-06), RenderJob (R2-P1-05) & RenderManifest (DAF-P0-04)
# ============================================================================


class IRBlock(StrictContractModel):
    """Canonical block in DocumentIR for cross-mode rendering consistency (DAF-P1-06)."""

    block_id: str
    section_id: str
    block_type: Literal[
        "heading",
        "paragraph",
        "list",
        "table",
        "equation",
        "figure_ref",
        "caption",
        "page_break",
    ]
    style_role: str = "body"
    text: str = ""
    level: int = 1
    list_items: list[str] = Field(default_factory=list)
    list_ordered: bool = False
    latex: str = ""
    table_headers: list[str] = Field(default_factory=list)
    table_rows: list[list[str]] = Field(default_factory=list)
    caption: str = ""
    source_refs: list[str] = Field(default_factory=list)


class IRSection(StrictContractModel):
    section_id: str
    title: str
    revision: int
    content_hash: str
    blocks: list[IRBlock] = Field(default_factory=list)


class DocumentIR(StrictContractModel):
    project_id: str
    title: str
    purpose: Literal["preview", "release"] = "release"
    sections: list[IRSection] = Field(default_factory=list)
    expected_equation_count: int = 0
    expected_table_count: int = 0


class RenderJob(StrictContractModel):
    """Recoverable two-phase file build job tracking (R2-P1-05 §5.3 & §7.1)."""

    job_id: str
    render_id: str
    project_id: str
    purpose: Literal["preview", "release"] = "release"
    phase: Literal[
        "prepared",
        "built",
        "promoted",
        "committed",
        "stale",
        "aborted_recovered",
        "failed",
    ] = "prepared"
    input_fingerprint: str
    temp_path: str = ""
    target_path: str = ""
    output_sha256: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    error_message: str = ""


class RenderManifest(StrictContractModel):
    """Snapshot-bound render manifest preventing stale exports (DAF-P0-04 §4.2 & R2-P1-05 §5.4)."""

    render_id: str
    job_id: str = ""
    purpose: Literal["preview", "release"]
    degradation_mode: Literal["strict", "preview_plaintext"] = "strict"
    input_fingerprint: str
    document_content_hash: str
    section_hashes: dict[str, str] = Field(default_factory=dict)
    outline_hash: str
    profile_hash: str
    template_sha256: str = ""
    section_mapping_hash: str = ""
    renderer_version: str = "daf-docx-ir-v2"
    docx_path: str
    artifact_path: str = ""
    docx_sha256: str
    output_sha256: str = ""
    artifact_status: Literal["committed", "stale", "superseded"] = "committed"
    omml_equations_count: int = 0
    tables_rendered: int = 0
    created_at: str = Field(default_factory=utc_now_iso)

    @model_validator(mode="after")
    def _sync_render_fields(self) -> "RenderManifest":
        if self.docx_path and not self.artifact_path:
            self.artifact_path = self.docx_path
        elif self.artifact_path and not self.docx_path:
            self.docx_path = self.artifact_path
        if self.docx_sha256 and not self.output_sha256:
            self.output_sha256 = self.docx_sha256
        elif self.output_sha256 and not self.docx_sha256:
            self.docx_sha256 = self.output_sha256
        return self


# ============================================================================
# 6. Approvals (DAF-P0-02 & R2-P0-01) & Project Workspace State
# ============================================================================


class ApprovalDecision(StrictContractModel):
    """Trusted human decision record written only by the trusted confirmation domain (R2-P0-01 §1.4 & §7.1)."""

    request_id: str
    decision: Literal["approved", "rejected"]
    human_actor_id: str
    confirmation_channel: Literal[
        "local_ui",
        "local_cli",
    ] = "local_cli"
    confirmed_at: str = Field(default_factory=utc_now_iso)
    artifact_revision: int
    artifact_hash: str
    audit_id: str
    comment: str = ""


class ApprovalRequest(StrictContractModel):
    """Non-privileged approval request visible to external Agents without any secret token (R2-P0-01 §1.4 & §7.1)."""

    approval_request_id: str
    project_id: str
    artifact_type: Literal["outline", "section", "final_draft"]
    target_id: str = "global"
    artifact_revision: int = 0
    artifact_hash: str
    status: Literal["pending", "approved", "rejected", "expired", "stale"] = "pending"
    requires_human_action: bool = True
    created_at: str = Field(default_factory=utc_now_iso)
    expires_at: str
    decision: ApprovalDecision | None = None


class ApprovalRecord(StrictContractModel):
    approval_id: str = ""
    request_id: str = ""
    artifact_type: Literal["outline", "section", "final_draft"]
    target_id: str = "global"
    status: Literal[
        "pending",
        "approved",
        "invalidated_by_edit",
        "invalidated_by_change",
        "rejected",
    ] = "pending"
    approved_revision: int = 0
    content_hash: str = ""
    artifact_hash: str = ""
    confirmed_via: Literal[
        "local_ui",
        "local_cli",
    ] = "local_cli"
    confirmed_by: str = "human_user"
    confirmed_at: str | None = None
    approved_by: str = "human_user"
    approved_at: str | None = None
    audit_id: str = ""
    invalidation_reason: str = ""
    comment: str = ""

    @model_validator(mode="after")
    def _sync_approval_hashes(self) -> "ApprovalRecord":
        if self.content_hash and not self.artifact_hash:
            self.artifact_hash = (
                f"sha256:{self.content_hash}"
                if not self.content_hash.startswith("sha256:")
                else self.content_hash
            )
        elif self.artifact_hash and not self.content_hash:
            self.content_hash = self.artifact_hash.removeprefix("sha256:")
        if self.approved_at and not self.confirmed_at:
            self.confirmed_at = self.approved_at
        elif self.confirmed_at and not self.approved_at:
            self.approved_at = self.confirmed_at
        return self


class ProjectBrief(StrictContractModel):
    project_id: str
    title: str
    profile_id: str
    domain: str = "general"
    research_direction: str = ""
    target_audience: str = "academic_reviewers"
    language: str = "zh-CN"
    created_at: str = Field(default_factory=utc_now_iso)
    global_constraints: list[str] = Field(default_factory=list)


class ProjectWorkspaceState(StrictContractModel):
    brief: ProjectBrief
    profile: DocumentProfileContract
    state_version: int = 0
    stage: Literal[
        "initialized",
        "sources_ingested",
        "outline_pending_approval",
        "outline_approved",
        "drafting_sections",
        "reviewing",
        "final_approved",
        "rendered",
        "exported",
    ] = "initialized"
    updated_at: str = Field(default_factory=utc_now_iso)
    source_registry: SourceRegistry = Field(default_factory=SourceRegistry)
    source_policy: ProjectSourcePolicy = Field(default_factory=ProjectSourcePolicy)
    evidence_ledger: EvidenceLedger = Field(default_factory=EvidenceLedger)
    outline_graph: OutlineGraph = Field(default_factory=OutlineGraph)
    terminology: TerminologyRegistry = Field(default_factory=TerminologyRegistry)
    decision_log: DecisionLog = Field(default_factory=DecisionLog)
    section_versions: SectionVersions = Field(default_factory=SectionVersions)
    review_ledger: ReviewLedger = Field(default_factory=ReviewLedger)
    approval_requests: dict[str, ApprovalRequest] = Field(default_factory=dict)
    approvals: dict[str, ApprovalRecord] = Field(default_factory=dict)
    render_jobs: dict[str, RenderJob] = Field(default_factory=dict)
    last_render_manifest: RenderManifest | None = None
    render_history: list[RenderManifest] = Field(default_factory=list)
    last_rendered_docx_path: str | None = None
    last_exported_pdf_path: str | None = None
    last_exported_pdf_source_docx_sha256: str | None = None
