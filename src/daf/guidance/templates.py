"""Versioned task methods. Profile section IDs, budgets and checklists live elsewhere."""

from dataclasses import dataclass

TEMPLATE_VERSION = "1.1"
SOURCE_SEARCH_MATCH_MODES = ("literal",)
SECTION_TASKS = {"draft_section", "revise_section", "review_section"}


@dataclass(frozen=True)
class GuidanceTemplate:
    goal: str
    tools: tuple[str, ...]
    required_inputs: tuple[str, ...]
    output_tool: str | None
    required_fields: tuple[str, ...]
    methods: tuple[str, ...]
    exit_criteria: tuple[str, ...]


TEMPLATES = {
    "source_analysis": GuidanceTemplate(
        "检查资料覆盖与缺口，区分已知事实、待验证内容与研究假设。",
        ("list_source_blocks", "search_sources", "get_source_block"),
        ("ProjectBrief", "SourceRegistry"),
        None,
        ("coverage", "evidence_gaps", "questions"),
        ("先列出授权来源，再逐页检索并按窗口读取；不要仅依赖摘要预览。",),
        ("覆盖清单中的资料可定位；缺口与未验证事实有明确标记。",),
    ),
    "evidence_extraction": GuidanceTemplate(
        "从获授权原文中提取可定位证据，登记精确摘录。",
        ("search_sources", "get_source_block", "register_evidence"),
        ("SourceBlockSlice",),
        "register_evidence",
        ("project_id", "evidence_items"),
        (
            "以 expected_sha256 读取当前切片，再复制原文 quote_excerpt；记录 source_id、slice_id 与 locator。",
            "L1 匹配只证明摘录存在；主张的充分支持需要 L2a/L2b 审核。",
        ),
        ("register_evidence 确认 L1 matched，且来源获准引用。",),
    ),
    "plan_outline": GuidanceTemplate(
        "建立论证主线、章节目标与证据分配，提交大纲供人工审批。",
        (
            "get_project_context",
            "submit_outline",
            "request_approval",
            "get_approval_status",
        ),
        ("DocumentProfileContract", "EvidenceLedger"),
        "submit_outline",
        ("project_id", "global_thesis", "nodes"),
        (
            "读取 plan_outline 上下文；从当前 Profile 获取必选章节，默认继承字数。",
            "提交后申请 outline 审批；已有有效或待处理申请时先检查状态。",
        ),
        ("大纲通过结构/证据校验，进入或已完成大纲人工审批。",),
    ),
    "draft_section": GuidanceTemplate(
        "依照当前大纲与证据完成章节正文，保留事实与假设的边界。",
        ("get_section_context", "search_sources", "get_source_block", "submit_section"),
        ("SectionContextContract", "OutlineGraph", "EvidenceLedger"),
        "submit_section",
        (
            "project_id",
            "section_id",
            "base_revision",
            "context_revision",
            "idempotency_key",
            "content_markdown",
            "summary",
            "claims",
            "source_bindings",
        ),
        (
            "先取 action=draft 的最新上下文，按有效字数与 required_elements 写作。",
            "formal 写作需大纲批准；预审批探索须显式使用 draft_preapproval。",
        ),
        ("提交通过字数、版本、用户禁改范围和证据校验。",),
    ),
    "revise_section": GuidanceTemplate(
        "按问题与用户决定定向修订章节，说明已解决的问题和残留缺口。",
        ("get_revision_tasks", "get_section_context", "submit_section"),
        ("ReviewIssue", "DecisionLog", "SectionContextContract"),
        "submit_section",
        (
            "project_id",
            "section_id",
            "base_revision",
            "context_revision",
            "idempotency_key",
            "content_markdown",
            "summary",
            "claims",
            "source_bindings",
        ),
        ("读取 action=revise 上下文及问题 ID，保持禁改范围；修订后重新审核。",),
        ("提交校验通过，变更说明对应问题 ID。",),
    ),
    "review_section": GuidanceTemplate(
        "逐项检查章节论证、来源与主张支持，保留否定或要求修订的结论。",
        (
            "get_section_content",
            "list_semantic_review_tasks",
            "claim_semantic_review_task",
            "submit_semantic_review",
            "accept_semantic_review",
        ),
        ("SemanticReviewTask", "ChecklistItemSpec", "EvidenceLedger"),
        "submit_semantic_review",
        (
            "project_id",
            "task_id",
            "section_revision",
            "context_fingerprint",
            "review_summary",
            "recommendation",
            "findings",
            "checklist_results",
            "checklist_hash",
        ),
        (
            "读取完整当前正文；任务的 checklist_items/checklist_hash 为权威清单。",
            "每项给出 verdict、rationale、section_locations、evidence_ids；另交 findings 和 claim_evaluations。",
            "recommendation=revise/block 必须保留；有失败项时修订后重新审查。",
        ),
        ("完整逐项结果经服务端校验；通过时 accept，否则有可执行的修订问题。",),
    ),
    "global_review": GuidanceTemplate(
        "检查跨章节论证、术语、证据与结构的一致性。",
        (
            "get_project_context",
            "get_section_content",
            "review_document",
            "get_revision_tasks",
        ),
        ("OutlineGraph", "SectionVersions", "TerminologyRegistry"),
        "review_document",
        ("project_id",),
        ("读取 global_review 上下文与全部必要章节；区分规则检查与语义审核。",),
        ("必要章节均已覆盖；问题对应章节及修订入口。",),
    ),
    "release_preflight": GuidanceTemplate(
        "核对当前发布门禁与产物状态，明确预览和正式交付条件。",
        (
            "get_project_status",
            "request_approval",
            "get_approval_status",
            "render_document",
            "export_document",
        ),
        ("ApprovalRecord", "ReviewLedger", "RenderManifest"),
        None,
        ("gate_status", "blocking_reasons", "next_actions"),
        (
            "purpose=preview 供中间检查；purpose=release 需有效大纲、语义审核、主张支持与终稿人工审批。",
            "缺少审批时请求可信人工确认；不得自行确认，不重复申请已有待办。",
        ),
        ("发布门禁实际通过，或准确列明阻断原因和恢复入口。",),
    ),
}


class GuidanceTemplateRegistry:
    version = TEMPLATE_VERSION
    templates = TEMPLATES
