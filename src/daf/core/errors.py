"""Structured Error Hierarchy and Error Codes for DocForge Agent Framework (DAF §7.1 & R2 v1.0)."""

from __future__ import annotations

from typing import Any


class DAFError(Exception):
    """Base exception for all deterministic DAF contract, concurrency, governance, and rendering errors."""

    default_code: str = "DAF_ERROR"
    default_retryable: bool = False
    default_recommended_tool: str | None = None

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        recommended_tool: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.retryable = self.default_retryable if retryable is None else retryable
        self.recommended_tool = (
            self.default_recommended_tool if recommended_tool is None else recommended_tool
        )
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "recommended_tool": self.recommended_tool,
                "details": self.details,
            },
        }


class UnsupportedGuidanceTaskError(DAFError):
    default_code = "UNSUPPORTED_GUIDANCE_TASK"
    default_recommended_tool = "get_capabilities"


class GuidanceTargetRequiredError(DAFError):
    default_code = "GUIDANCE_TARGET_REQUIRED"
    default_recommended_tool = "get_document_profile"


class GuidanceBudgetExceededError(DAFError):
    default_code = "GUIDANCE_BUDGET_EXCEEDED"
    default_recommended_tool = "get_task_guidance"


class SourceScopeDeniedError(DAFError):
    default_code = "SOURCE_SCOPE_DENIED"
    default_recommended_tool = "get_source_policy"


class SourcePolicyChangeRequiresHumanError(DAFError):
    default_code = "SOURCE_POLICY_CHANGE_REQUIRES_HUMAN"
    default_recommended_tool = "get_source_policy"


class PolicyConflictError(DAFError):
    default_code = "POLICY_CONFLICT"
    default_recommended_tool = "get_source_policy"


class SecurityBoundaryError(DAFError, ValueError):
    """Raised when a project_id or file path violates workspace security boundaries."""

    default_code = "SECURITY_BOUNDARY_VIOLATION"
    default_retryable = False


class ArtifactPathInvalidError(SecurityBoundaryError):
    """Raised when an output filename or artifact path attempts traversal or invalid syntax."""

    default_code = "ARTIFACT_PATH_INVALID"
    default_retryable = False


class ProjectAlreadyExistsError(DAFError, ValueError):
    """Raised when create_project is called with an already-existing project_id."""

    default_code = "PROJECT_ALREADY_EXISTS"
    default_retryable = False
    default_recommended_tool = "get_project_status"


class ContractValidationError(DAFError, ValueError):
    """Raised when an Agent submission violates a DAF contract, evidence rule, or user constraint."""

    default_code = "CONTRACT_VALIDATION_ERROR"
    default_retryable = True


class RevisionConflictError(DAFError, ValueError):
    """Raised when base_revision or SQLite state_version conflicts on concurrent update."""

    default_code = "REVISION_CONFLICT"
    default_retryable = True
    default_recommended_tool = "get_section_context"


class ContextStaleError(DAFError, ValueError):
    """Raised when submission.context_revision does not match current section dependency fingerprint."""

    default_code = "CONTEXT_STALE"
    default_retryable = True
    default_recommended_tool = "get_section_context"


class IdempotencyKeyConflictError(DAFError, ValueError):
    """Raised when the same idempotency_key is reused with a different request payload."""

    default_code = "IDEMPOTENCY_KEY_REUSED"
    default_retryable = False


class ApprovalConflictError(DAFError, ValueError):
    """Raised when an approval gate is missing, stale, or lacks verifiable human confirmation."""

    default_code = "APPROVAL_REQUIRED"
    default_retryable = True
    default_recommended_tool = "request_approval"


class UntrustedApprovalChannelError(ApprovalConflictError):
    """Raised when an Agent attempts to mint or consume human approval tokens or invoke trusted confirmation channels."""

    default_code = "UNTRUSTED_APPROVAL_CHANNEL"
    default_retryable = False
    default_recommended_tool = "request_approval"


class SemanticReviewRequiredError(ApprovalConflictError):
    """Raised when release or approval is attempted without an accepted semantic review for the current revision."""

    default_code = "SEMANTIC_REVIEW_REQUIRED"
    default_retryable = True
    default_recommended_tool = "submit_semantic_review"


class SemanticReviewStaleError(ApprovalConflictError):
    """Raised when an existing semantic review is stale relative to section, evidence, or constraint changes."""

    default_code = "SEMANTIC_REVIEW_STALE"
    default_retryable = True
    default_recommended_tool = "review_document"


class ClaimReviewPendingError(ApprovalConflictError):
    """Raised when factual claims have passed L2a rules but lack accepted L2b semantic support review."""

    default_code = "CLAIM_REVIEW_PENDING"
    default_retryable = True
    default_recommended_tool = "submit_semantic_review"


class EvidenceChangedError(ApprovalConflictError):
    """Raised when bound evidence revision, fingerprint, or quote_hash changed after a claim or section was reviewed."""

    default_code = "EVIDENCE_CHANGED"
    default_retryable = True
    default_recommended_tool = "review_document"


class RenderStaleError(DAFError, RuntimeError):
    """Raised when an existing rendered artifact does not match the current RenderManifest fingerprint."""

    default_code = "RENDER_STALE"
    default_retryable = True
    default_recommended_tool = "render_document"


class TemplateRenderError(DAFError, RuntimeError):
    """Raised when template rendering fails or mapped anchors are invalid/ambiguous."""

    default_code = "TEMPLATE_MAPPING_INVALID"
    default_retryable = False


class PandocUnavailableError(TemplateRenderError):
    """Raised when native OMML equation rendering requires Pandoc in strict mode."""

    default_code = "RENDER_DEPENDENCY_MISSING"
    default_retryable = False


class PDFExportError(DAFError, RuntimeError):
    """Raised when PDF export via Windows Word COM fails or is unavailable."""

    default_code = "PDF_EXPORT_FAILED"
    default_retryable = True


class SourceSliceNotFoundError(ContractValidationError, KeyError):
    """Raised when a requested source_id or slice_id does not exist in the current project."""

    default_code = "SOURCE_SLICE_NOT_FOUND"
    default_retryable = False
    default_recommended_tool = "list_source_blocks"


class SourceSliceStaleError(ContextStaleError):
    """Raised when caller's expected_sha256 does not match the current slice SHA-256."""

    default_code = "SOURCE_SLICE_STALE"
    default_retryable = True
    default_recommended_tool = "list_source_blocks"


class SourceIndexStaleError(ContextStaleError):
    """Raised when a pagination cursor's source index revision is outdated after source re-ingestion."""

    default_code = "SOURCE_INDEX_STALE"
    default_retryable = True
    default_recommended_tool = "list_source_blocks"


class SourceQueryInvalidError(ContractValidationError):
    """Raised when source search query, window offsets, cursor, or pagination parameters are invalid."""

    default_code = "SOURCE_QUERY_INVALID"
    default_retryable = True
    default_recommended_tool = None


class UnsupportedTaskTypeError(ContractValidationError):
    """Raised when get_project_context is called with an unsupported task_type."""

    default_code = "UNSUPPORTED_TASK_TYPE"
    default_retryable = False
    default_recommended_tool = "get_project_context"


class WordBudgetInvalidError(ContractValidationError):
    """Raised when a section's effective word budget has invalid min_words/max_words bounds."""

    default_code = "WORD_BUDGET_INVALID"
    default_retryable = True
    default_recommended_tool = "get_document_profile"


class ContextBudgetExceededError(ContractValidationError):
    """Raised when core required context fields exceed requested max_context_chars or budget args conflict."""

    default_code = "CONTEXT_BUDGET_EXCEEDED"
    default_retryable = True
    default_recommended_tool = None


class ReviewChecklistIncompleteError(ContractValidationError):
    """Raised when semantic review checklist_results are incomplete, duplicated, or mismatched with task checklist_hash."""

    default_code = "REVIEW_CHECKLIST_INCOMPLETE"
    default_retryable = True
    default_recommended_tool = "list_semantic_review_tasks"


class ProjectNotFoundError(DAFError, KeyError):
    """Raised when a requested project_id does not exist in SQLite."""

    default_code = "PROJECT_NOT_FOUND"
    default_retryable = False
    default_recommended_tool = "create_project"


class SectionNotFoundError(DAFError, KeyError):
    """Raised when a requested section_id does not exist in the project."""

    default_code = "SECTION_NOT_FOUND"
    default_retryable = False
    default_recommended_tool = "get_project_status"


class StateCorruptedError(DAFError, RuntimeError):
    """Raised when SQLite state references a missing content blob or inconsistent state."""

    default_code = "STATE_CORRUPTED"
    default_retryable = False
    default_recommended_tool = None


class BibliographyMissingError(ContractValidationError):
    """Raised when release is attempted while cited sources lack user-confirmed bibliography."""

    default_code = "BIBLIOGRAPHY_MISSING"
    default_retryable = False
    default_recommended_tool = "propose_source_bibliography"


class CrossrefUnresolvedError(ContractValidationError):
    """Raised when release is attempted while cross-references point to undefined labels."""

    default_code = "CROSSREF_UNRESOLVED"
    default_retryable = True
    default_recommended_tool = "submit_section"
