---
name: docforge-agent-framework
description: >
  Local single-user writing guardrail framework for AI Agents: inline Pandoc citation
  verification, user-owned YAML profiles, independent quote-anchored review, local
  approval UI, and single-pass Pandoc OMML/CSL DOCX compilation.
---

# DAF 外部 Agent 路由指南（协议 v2.0）

DAF 是本地单用户写作护栏：管理状态、内联证据校验、审稿独立与本地人工审批；外部 Agent 负责研究、写作与语义判断。

1. 先调用 `get_capabilities` 获取紧凑能力摘要（< 3000 字符；需要契约详情时传 `include_schemas`）。当前仅支持 `builtin` 与 `mapped` 两种模板模式（`tagged` 模式已移除，传入触发 `TEMPLATE_MODE_UNSUPPORTED`）。
2. 已有项目先 `get_project_status`；新项目用 `create_project` 选择用户在 `<workspace>/profiles/<id>.yaml` 定义的 Profile（Agent 不可覆盖 Profile 规则，支持在 Profile `content.extra_physical_units` 扩展计量单位）。
3. 每次进入新阶段可调用 `get_task_guidance` 或直接按写工具返回的 `next_action` 推进。
4. 在 `DAF_ALLOWED_SOURCE_ROOTS` 允许目录内 `ingest_sources`，通过 `find_sources` + `read_source_block` 阅读原文，再用 `register_evidence` 登记精确 `quote_excerpt`（L3 默认 `unassessed`，仅用户可标 `trusted`）。
5. 外部文献著录治理：Agent 可通过 `propose_source_bibliography` 提交建议的 CSL-JSON 元数据，但**必须经用户通过 `daf ui`（Adopt Proposal / 编辑确认）或 `daf --workspace <dir> sources set-bib <project_id> <source_id> --bibtex <file> / --csl-json <file>` 确认**；若文献引用了未确认来源，正式版导出（`purpose="release"`）会被 `BIBLIOGRAPHY_MISSING` 阻断，而 `purpose="preview"` 会渲染可见占位文本。
6. `submit_outline` 提交大纲 → `request_approval(artifact_type="outline")` → 等待用户在本地 `daf ui` 或 `daf approvals decide` 审批。**大纲批准后，仅允许向大纲内声明的 `section_id` 提交正文，未列章节会触发 `SECTION_NOT_IN_OUTLINE`。严禁代用户运行审批命令。**
7. `get_section_context` → `submit_section`（必传长度 ≥ 3 的非空白 `author_session`）：
   - 事实性数据/比较句必须使用 Pandoc 原生内联引用 `[@ev_01]` / `[@ev_01; @ev_02]`：收窄后的 R-A 将未引用的“数字+物理/计量单位或弱比较词+数字”判为 `major`（阻塞发布），将纯强比较词判为 `minor`（提示但不阻断发布），规划/计数句式豁免；R-B 按分句作用域（含 `[@ev_01; @ev_02]` 并集）严格校验数字与换算后的单位一致性；
   - 研究假设句末加 `[@hypothesis]` 且不得使用“实验表明/结果显示”等伪实验表述（R-D）；
   - 交叉引用统一使用 Pandoc 原生语法：表格推荐用带围栏块 `::: {#tbl:id} ... :::`（使用 `: Caption {#tbl:id}` 会在 `submit_section` 返回 `minor` 移植性提示）、图片用 `![caption](path){#fig:id}`、公式用 `[$$ ... $$]{#eq:id}`，正文用 `@tbl:id` / `@fig:id` / `@eq:id` 引用；正式版存在未定义交叉引用标签会触发 `CROSSREF_UNRESOLVED`。
8. 审稿必须由宿主启动的**独立子 Agent 或新会话**完成，不读取作者写作过程：
   - `start_review(reviewer_id=<独立会话ID>)` 建任务并领取（`reviewer_id` / `reviewed_by` 均必须为长度 ≥ 3 的非空白独立标识，缺失触发 `REVIEWER_ID_REQUIRED`，与 `author_session` 忽略大小写相同时触发 `REVIEWER_IS_AUTHOR`）；
   - `submit_review` 逐项提交 `checklist_results`（非 `not_applicable` 项 `rationale` ≥ 15 字且多项间不得完全复制同一条理由，必须附带正文 ≥ 10 归一化字符或整句的 `quotes`，且有效项 > 3 条时不得全部复用同一引用句；`not_applicable` 项免引用但 `rationale` ≥ 20 字）、显式 `recommendation`、`findings` 与全量 `claim_evaluations`；合法 `approve` 报告提交即自动验收。
9. 需记录待用户确认的建议时调用 `record_agent_note`（不会修改用户约束或模板冲突确认）。如需检查工作区 SQLite 状态与内容寻址 `blobs` 完整性，可运行 `daf --workspace <dir> doctor <project_id>`。
10. 终稿经用户在 `daf ui` / `daf approvals decide` 批准后，调用 `render_document(purpose="release")` 与 `export_document(purpose="release")` 输出最终 DOCX/PDF。
