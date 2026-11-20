# DocForge Agent Framework：面向外部 Agent 的专业文书执行框架

> **文档类型**：新项目设计说明书 / 架构与实施基线  
> **项目名称**：DocForge Agent Framework（简称 **DAF**）  
> **项目来源**：由既有 [DocForge](https://github.com/1inke/docforge) 项目派生的新项目  
> **文档版本**：设计草案 v1.0  
> **编写日期**：2026-11-07  

## 1. 核心定位

**DAF 是一个 Agent-native Document Engineering Framework：面向外部智能体（Codex / Antigravity / Claude Code 等）开放的、状态化、规范驱动、证据可追溯、支持人工协作的文书工程执行框架。**

- **外部 Agent 负责**：理解需求、选择策略、识别与拆分原始材料、生成章节大纲、专业内容写作与推理、语义性审稿、与用户自然语言对话。
- **DAF 负责**：提供能力发现 (`Capability Registry`)、三层文书规范 (`Document Profile`: Content / Structure / Layout)、面向章节与动作的工作记忆与上下文包 (`Context Manager`)、带版本与审批级联失效的工作流状态机 (`Workflow Engine`)、确定性规则校验与修订任务 (`Quality & Revision`)、以及原生 OMML 公式与 Word 模板映射引擎 (`Document Engine`)。

## 2. 十五个核心 Agent 接口

1. `get_capabilities`：查询可用接口、支持的文书类型、协议版本、状态约束与本地环境（Pandoc / Word COM）可用性。
2. `get_document_profile`：获取指定文书类型的 Content / Structure / Layout 三层规范。
3. `create_project`：新建受控项目工作区与 SQLite 状态库。
4. `ingest_sources`：解析材料（MD/TXT/DOCX/PDF）为可定位分块、校验证据摘录、探测只读 Word 模板结构。
5. `get_project_context`：获取项目全局简报 (`ProjectBrief`)、决策记录 (`DecisionLog`)、术语表 (`TerminologyRegistry`) 与当前版本概览。
6. `submit_outline`：提交章节树 (`OutlineGraph`)、章节契约与依赖关系（自动递增 `outline_revision` 并使旧审批失效）。
7. `get_section_context`：按 `project_id`、`section_id`、`action` 和预算生成 `SectionContextContract`（含已核验证据、上游依赖摘要、非可信资料边界标记）。
8. `submit_section`：按 `SectionSubmissionContract` 提交章节正文、结构化块与证据绑定（检查 `base_revision` 乐观锁与 `idempotency_key` 幂等性，计算版本 diff，触发下游审批失效）。
9. `review_document`：执行确定性 QA 校验并生成待外部 Agent 执行的语义审查任务契约，写入 `ReviewLedger`。
10. `get_revision_tasks`：查询当前待处理的 `ReviewIssue` 列表与受影响需重审的章节。
11. `submit_feedback`：记录用户自然语言反馈、修改禁区或外部 Agent 的语义审稿结论至 `DecisionLog` / `ReviewLedger`。
12. `approve_artifact`：对大纲、特定章节或整篇成稿的具体版本号/内容哈希进行批准或驳回。
13. `get_project_status`：跨会话恢复入口，返回当前阶段、各资源版本号、审批有效性与下一步可执行动作。
14. `render_document`：将已批准的结构化章节映射至目标 Word 模板或新建文档，通过 Pandoc 编译原生 OMML 公式并执行渲染后结构检查。
15. `export_document`：输出最终交付包（DOCX、可选 Word COM 导出 PDF、QA 报告与修订记录清单）。
