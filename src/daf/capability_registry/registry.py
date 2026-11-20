"""Capability Registry for DocForge Agent Framework (DAF §3.1, §5.1 & Remediation R2 v1.0)."""

from __future__ import annotations

import shutil
import sys
from typing import Any

from daf.core.contracts import (
    ApprovalRequest,
    ChecklistItemResult,
    ChecklistItemSpec,
    ContextBudgetMetadata,
    DependencySnapshot,
    DocumentIR,
    DocumentProfileContract,
    ProjectSourcePolicy,
    RenderManifest,
    ReviewIssue,
    SectionContextContract,
    SectionSubmissionContract,
    SemanticReviewReport,
    TaskGuidanceContract,
)
from daf.document_profiles.profiles import get_document_profile, list_profile_summaries
from daf.guidance.templates import SOURCE_SEARCH_MATCH_MODES, TEMPLATES

PROTOCOL_VERSION = "1.4"

ERROR_CODE_CATALOG: list[dict[str, Any]] = [
    {
        "code": "REVISION_CONFLICT",
        "retryable": True,
        "recommended_tool": "get_section_context",
        "description": "Optimistic lock conflict on section base_revision or project SQLite state_version CAS.",
    },
    {
        "code": "CONTEXT_STALE",
        "retryable": True,
        "recommended_tool": "get_section_context",
        "description": "Submitted context_revision / context_fingerprint no longer matches current section dependency snapshot.",
    },
    {
        "code": "IDEMPOTENCY_KEY_REUSED",
        "retryable": False,
        "recommended_tool": None,
        "description": "Same idempotency_key was submitted with a different semantic request payload.",
    },
    {
        "code": "PROJECT_ALREADY_EXISTS",
        "retryable": False,
        "recommended_tool": "get_project_status",
        "description": "create_project was called with an already-existing project_id (create-only semantics).",
    },
    {
        "code": "APPROVAL_PENDING",
        "retryable": True,
        "recommended_tool": "get_approval_status",
        "description": "Human approval request is pending user action in the trusted confirmation portal.",
    },
    {
        "code": "APPROVAL_REQUIRED",
        "retryable": True,
        "recommended_tool": "request_approval",
        "description": "Mandatory human approval gate ('outline' or 'final_draft') or review completion is missing.",
    },
    {
        "code": "APPROVAL_STALE",
        "retryable": True,
        "recommended_tool": "request_approval",
        "description": "Existing approval or request was bound to an older revision/hash that has since been modified.",
    },
    {
        "code": "UNTRUSTED_APPROVAL_CHANNEL",
        "retryable": False,
        "recommended_tool": "request_approval",
        "description": "Agent attempted to mint or consume human approval tokens or invoke restricted confirmation channels.",
    },
    {
        "code": "SEMANTIC_REVIEW_REQUIRED",
        "retryable": True,
        "recommended_tool": "submit_semantic_review",
        "description": "Section lacks an accepted SemanticReviewReport for its current revision and content_hash.",
    },
    {
        "code": "SEMANTIC_REVIEW_STALE",
        "retryable": True,
        "recommended_tool": "review_document",
        "description": "Previously submitted/accepted semantic review became stale after section, evidence, or constraint changes.",
    },
    {
        "code": "EVIDENCE_CHANGED",
        "retryable": True,
        "recommended_tool": "review_document",
        "description": "Bound EvidenceItem revision, fingerprint, or quote_hash changed after section drafting or review.",
    },
    {
        "code": "CLAIM_REVIEW_PENDING",
        "retryable": True,
        "recommended_tool": "submit_semantic_review",
        "description": "Factual claim passed L2a deterministic rule checks but lacks an accepted L2b semantic support review.",
    },
    {
        "code": "ARTIFACT_PATH_INVALID",
        "retryable": False,
        "recommended_tool": None,
        "description": "output_filename or artifact path contains path traversal, drive/stream syntax, or reserved device names.",
    },
    {
        "code": "RENDER_STALE",
        "retryable": True,
        "recommended_tool": "render_document",
        "description": "Existing rendered DOCX does not match current RenderManifest input_fingerprint or on-disk SHA-256.",
    },
    {
        "code": "EVIDENCE_UNVERIFIED",
        "retryable": True,
        "recommended_tool": "register_evidence",
        "description": "Bound evidence_id does not exist or failed L1 normalized substring verification.",
    },
    {
        "code": "CLAIM_UNSUPPORTED",
        "retryable": True,
        "recommended_tool": "review_document",
        "description": "Claim–Evidence binding failed L2a rule check or L2b semantic support review.",
    },
    {
        "code": "TEMPLATE_MAPPING_INVALID",
        "retryable": False,
        "recommended_tool": None,
        "description": "Template mapping target is missing, out of range, duplicated, or structurally invalid.",
    },
    {
        "code": "RENDER_DEPENDENCY_MISSING",
        "retryable": False,
        "recommended_tool": None,
        "description": "Pandoc executable is missing or failed while compiling LaTeX equations in strict mode.",
    },
    {
        "code": "SOURCE_SLICE_NOT_FOUND",
        "retryable": False,
        "recommended_tool": "list_source_blocks",
        "description": "Requested source_id or slice_id does not exist in the current project.",
    },
    {
        "code": "SOURCE_SLICE_STALE",
        "retryable": True,
        "recommended_tool": "list_source_blocks",
        "description": "Caller's expected_sha256 does not match the current slice SHA-256.",
    },
    {
        "code": "SOURCE_INDEX_STALE",
        "retryable": True,
        "recommended_tool": "list_source_blocks",
        "description": "Pagination cursor's source index revision is outdated after source re-ingestion.",
    },
    {
        "code": "SOURCE_QUERY_INVALID",
        "retryable": True,
        "recommended_tool": None,
        "description": "Source search query, character window offsets, cursor, or pagination parameters are invalid.",
    },
    {
        "code": "UNSUPPORTED_TASK_TYPE",
        "retryable": False,
        "recommended_tool": "get_project_context",
        "description": "get_project_context was called with an unsupported task_type (must be 'plan_outline' or 'global_review').",
    },
    {
        "code": "WORD_BUDGET_INVALID",
        "retryable": True,
        "recommended_tool": "get_document_profile",
        "description": "Section word budget has invalid bounds (min_words < 0, max_words <= 0, or min_words > max_words).",
    },
    {
        "code": "CONTEXT_BUDGET_EXCEEDED",
        "retryable": True,
        "recommended_tool": None,
        "description": "Minimum required core context fields exceed requested max_context_chars, or budget parameters conflict.",
    },
    {
        "code": "REVIEW_CHECKLIST_INCOMPLETE",
        "retryable": True,
        "recommended_tool": "list_semantic_review_tasks",
        "description": "Semantic review checklist_results are missing, duplicated, or mismatched with task checklist_hash.",
    },
]

ERROR_CODE_CATALOG.extend([
    {"code": code, "retryable": False, "recommended_tool": tool, "description": description}
    for code, tool, description in [
        ("UNSUPPORTED_GUIDANCE_TASK", "get_capabilities", "Unknown guidance task; use supported task kinds."),
        ("GUIDANCE_TARGET_REQUIRED", "get_document_profile", "A valid section_id is required for section guidance."),
        ("GUIDANCE_BUDGET_EXCEEDED", "get_task_guidance", "Mandatory guidance cannot fit the requested character budget."),
        ("SOURCE_SCOPE_DENIED", "get_source_policy", "Source operation is outside the effective project allowlist."),
        ("SOURCE_POLICY_CHANGE_REQUIRES_HUMAN", "get_source_policy", "Source policy changes can only be performed by the local user via UI or CLI."),
        ("POLICY_CONFLICT", "get_source_policy", "Policy rules conflict or the decision request is stale."),
        ("SOURCE_IMPORT_NOT_CONFIGURED", "get_capabilities", "Trusted deployment must configure import roots before local file ingestion."),
        ("SOURCE_IMPORT_CONFIG_INVALID", "get_capabilities", "Import roots must be a JSON array of existing directories."),
        ("SOURCE_IMPORT_PATH_INVALID", None, "Source/template path is invalid, unavailable or changed during safe opening."),
        ("SOURCE_TYPE_UNSUPPORTED", None, "Local source or template file extension is unsupported."),
    ]
])

TOOL_CATALOG: list[dict[str, Any]] = [
    {"name": "get_task_guidance", "category": "guidance", "mutates_state": False, "idempotent": True,
     "description": "Read deterministic, bounded task guidance from current Profile, source policy, decisions and workflow.",
     "task_kinds": list(TEMPLATES)},
    {"name": "get_source_policy", "category": "governance", "mutates_state": False, "idempotent": True,
     "description": "Read effective source permissions and external capability status."},
    {
        "name": "get_capabilities",
        "category": "discovery",
        "mutates_state": False,
        "idempotent": True,
        "description": "Discover DAF protocol version, supported document profiles, tool catalog, error codes, contract schemas, and runtime dependencies.",
    },
    {
        "name": "get_document_profile",
        "category": "discovery",
        "mutates_state": False,
        "idempotent": True,
        "description": "Retrieve the complete 3-layer DocumentProfileContract (Content, Structure, Layout) for a profile_id.",
    },
    {
        "name": "create_project",
        "category": "lifecycle",
        "mutates_state": True,
        "idempotent": False,
        "description": "Initialize an isolated project workspace with create-only semantics (raises PROJECT_ALREADY_EXISTS if project_id exists).",
    },
    {
        "name": "update_profile_settings",
        "category": "lifecycle",
        "mutates_state": True,
        "idempotent": True,
        "description": "Safely update project DocumentProfileContract settings while preserving mandatory approval gates.",
    },
    {
        "name": "ingest_sources",
        "category": "evidence",
        "mutates_state": True,
        "idempotent": True,
        "description": "Parse local source files (.md/.txt/.docx/.pdf) and optional .docx template into traceable SourceBlockSlices; cascades invalidation if a source file changes.",
    },
    {
        "name": "list_source_blocks",
        "category": "evidence",
        "mutates_state": False,
        "idempotent": True,
        "description": "List ingested SourceBlockSlice metadata (source_id, slice_id, locator, char_count, sha256) with stable pagination.",
    },
    {
        "name": "get_source_block",
        "category": "evidence",
        "mutates_state": False,
        "idempotent": True,
        "description": "Read a character window [start_char, end_char) of raw text from an ingested SourceBlockSlice with optional expected_sha256 verification.",
    },
    {
        "name": "search_sources",
        "supported_match_modes": list(SOURCE_SEARCH_MATCH_MODES),
        "category": "evidence",
        "mutates_state": False,
        "idempotent": True,
        "description": "Perform deterministic literal substring search across ingested SourceBlockSlices, returning locators, offsets, and verbatim snippets.",
    },
    {
        "name": "register_evidence",
        "category": "evidence",
        "mutates_state": True,
        "idempotent": True,
        "description": "Register or update versioned EvidenceItems (L1 excerpt_match_status, L3 source_reliability) with automatic cascade invalidation when evidence changes.",
    },
    {
        "name": "get_project_context",
        "category": "context",
        "mutates_state": False,
        "idempotent": True,
        "description": "Fetch task-aware, character-budgeted project context for 'plan_outline' or 'global_review' with explicit ContextBudgetMetadata.",
    },
    {
        "name": "submit_outline",
        "category": "authoring",
        "mutates_state": True,
        "idempotent": False,
        "description": "Validate and commit the structured OutlineGraph (inheriting Profile word budgets unless explicitly overridden), TerminologyRegistry, and key claims.",
    },
    {
        "name": "get_section_context",
        "category": "context",
        "mutates_state": False,
        "idempotent": True,
        "description": "Assemble a character-budgeted SectionContextContract with deterministic context_fingerprint, DependencySnapshot, and ContextBudgetMetadata.",
    },
    {
        "name": "get_section_content",
        "category": "context",
        "mutates_state": False,
        "idempotent": True,
        "description": "Read drafted section markdown in controlled character windows [start_char, end_char) with optional expected_revision check.",
    },
    {
        "name": "submit_section",
        "category": "authoring",
        "mutates_state": True,
        "idempotent": True,
        "description": "Validate and persist a section draft inside a single SQLite CAS transaction checking outline gate, base_revision, context_revision fingerprint, idempotency_key + request_hash, and L1/L2a evidence bindings.",
    },
    {
        "name": "review_document",
        "category": "review",
        "mutates_state": True,
        "idempotent": False,
        "description": "Run deterministic QA rules (L1/L2a) and create pending SemanticReviewTasks for each drafted section.",
    },
    {
        "name": "list_semantic_review_tasks",
        "category": "review",
        "mutates_state": False,
        "idempotent": True,
        "description": "List semantic review tasks (pending/claimed/submitted/accepted/stale) and pending L2b claim bindings.",
    },
    {
        "name": "claim_semantic_review_task",
        "category": "review",
        "mutates_state": True,
        "idempotent": True,
        "description": "Claim a pending semantic review task before submitting a structured SemanticReviewReport.",
    },
    {
        "name": "submit_semantic_review",
        "category": "review",
        "mutates_state": True,
        "idempotent": False,
        "description": "Submit a structured SemanticReviewReport and L2b claim_evaluations bound to section_revision and context_fingerprint.",
    },
    {
        "name": "accept_semantic_review",
        "category": "review",
        "mutates_state": True,
        "idempotent": True,
        "description": "Validate a submitted SemanticReviewReport against live section/evidence hashes and L2a rules, then transition to accepted.",
    },
    {
        "name": "get_revision_tasks",
        "category": "review",
        "mutates_state": False,
        "idempotent": True,
        "description": "Return actionable revision tasks grouped by section from open ReviewLedger issues, pending semantic reviews, and pending L2b claim reviews.",
    },
    {
        "name": "submit_feedback",
        "category": "governance",
        "mutates_state": True,
        "idempotent": False,
        "description": "Persist user feedback, style preferences, or forbidden modification scopes into DecisionLog and open targeted revision issues.",
    },
    {
        "name": "request_approval",
        "category": "governance",
        "mutates_state": True,
        "idempotent": False,
        "description": "Create a non-privileged pending ApprovalRequest for 'outline', 'section', or 'final_draft' (never returns any secret confirmation token).",
    },
    {
        "name": "get_approval_status",
        "category": "governance",
        "mutates_state": False,
        "idempotent": True,
        "description": "Query the current status ('pending', 'approved', 'rejected', 'expired', 'stale') of an ApprovalRequest.",
    },
    {
        "name": "get_project_status",
        "category": "lifecycle",
        "mutates_state": False,
        "idempotent": True,
        "description": "Inspect full cross-session project state, state_version, section revisions, approval states, RenderManifest, and recommended next actions.",
    },
    {
        "name": "render_document",
        "category": "rendering",
        "mutates_state": True,
        "idempotent": False,
        "description": "Compile DocumentIR into a native Word (.docx) document via recoverable out-of-transaction RenderJob and RenderManifest.",
    },
    {
        "name": "export_document",
        "category": "rendering",
        "mutates_state": True,
        "idempotent": False,
        "description": "Verify committed RenderManifest fingerprint and DOCX SHA-256 (auto-rerendering if stale), then export DOCX, optional PDF, audit trail, and 3-layer evidence traceability matrix.",
    },
]


class CapabilityRegistry:
    """Provides self-describing capability discovery for external Agents."""

    @staticmethod
    def detect_environment() -> dict[str, Any]:
        pandoc_path = shutil.which("pandoc")
        word_com_available = False
        if sys.platform == "win32":
            try:
                import win32com.client  # noqa: F401

                word_com_available = True
            except Exception:
                word_com_available = False

        return {
            "platform": sys.platform,
            "python_version": sys.version.split()[0],
            "pandoc_available": pandoc_path is not None,
            "pandoc_path": pandoc_path,
            "word_com_pdf_available": word_com_available,
            "supported_source_extensions": [".md", ".txt", ".docx", ".pdf"],
            "supported_template_modes": ["builtin", "tagged", "mapped"],
            "supported_export_formats": ["docx", "pdf", "json_bundle"],
        }

    @classmethod
    def get_capabilities(cls, profile_id: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "framework_name": "DocForge Agent Framework (DAF)",
            "protocol_version": PROTOCOL_VERSION,
            "architecture_mode": "agent_driven_framework_enforced",
            "source_import_boundary": {"configuration_env": "DAF_ALLOWED_SOURCE_ROOTS", "format": "json_array",
                                       "unconfigured": "deny", "empty_list": "deny", "template_uses_same_boundary": True},
            "guidance": {"schema_version": "guidance.v1", "task_kinds": list(TEMPLATES),
                         "external_lookup_available": False, "default_source_mode": "project_only"},
            "supported_profiles": list_profile_summaries(),
            "tools": TOOL_CATALOG,
            "error_codes": ERROR_CODE_CATALOG,
            "approval_gates": [
                {
                    "artifact_type": "outline",
                    "description": "Requires human confirmation in the trusted confirmation portal before formal section drafting.",
                    "binds_to": ["outline_revision", "outline_hash", "approval_request_id"],
                },
                {
                    "artifact_type": "section",
                    "description": "Optional per-section human confirmation bound to section revision and content_hash.",
                    "binds_to": ["revision", "content_hash", "approval_request_id"],
                },
                {
                    "artifact_type": "final_draft",
                    "description": "Mandatory final approval gate over all required sections and accepted semantic reviews before purpose='release' render or export.",
                    "binds_to": ["all_required_section_revisions", "aggregate_content_hash", "approval_request_id"],
                },
            ],
            "concurrency_and_idempotency": {
                "sqlite_atomic_cas": "All state mutations run in BEGIN IMMEDIATE transactions with state_version CAS.",
                "optimistic_locking": "submit_section requires base_revision matching current section revision.",
                "context_fingerprint": "submit_section requires context_revision matching current task-scoped DependencySnapshot.",
                "idempotency": "submit_section deduplicates repeated calls with the same (idempotency_key, request_hash) and rejects payload reuse with IDEMPOTENCY_KEY_REUSED.",
                "approval_invalidation": (
                    "Any mutation to an approved outline, section, evidence item, or source file automatically invalidates its approval "
                    "and marks downstream dependent sections as needs_revalidation=True."
                ),
            },
            "environment": cls.detect_environment(),
            "contract_schemas": {
                "TaskGuidanceContract": TaskGuidanceContract.model_json_schema(),
                "ProjectSourcePolicy": ProjectSourcePolicy.model_json_schema(),
                "DocumentProfileContract": DocumentProfileContract.model_json_schema(),
                "SectionContextContract": SectionContextContract.model_json_schema(),
                "ContextBudgetMetadata": ContextBudgetMetadata.model_json_schema(),
                "DependencySnapshot": DependencySnapshot.model_json_schema(),
                "SectionSubmissionContract": SectionSubmissionContract.model_json_schema(),
                "SemanticReviewReport": SemanticReviewReport.model_json_schema(),
                "ChecklistItemSpec": ChecklistItemSpec.model_json_schema(),
                "ChecklistItemResult": ChecklistItemResult.model_json_schema(),
                "ApprovalRequest": ApprovalRequest.model_json_schema(),
                "ReviewIssue": ReviewIssue.model_json_schema(),
                "RenderManifest": RenderManifest.model_json_schema(),
                "DocumentIR": DocumentIR.model_json_schema(),
            },
        }
        if profile_id is not None:
            profile = get_document_profile(profile_id)
            result["requested_profile"] = profile.model_dump(mode="json")
        return result
