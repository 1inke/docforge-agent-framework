---
name: docforge-agent-framework
description: >
  Local single-user writing guardrail framework for AI Agents: inline Pandoc citation
  verification, user-owned YAML profiles, independent quote-anchored review, local
  approval UI, and single-pass Pandoc OMML/CSL DOCX compilation.
---

# DAF 外部 Agent 路由指南（协议 v2.0）

DAF 是本地单用户写作护栏：管理状态、内联证据校验、审稿独立与本地人工审批；外部 Agent 负责研究、写作与语义判断。

1. 先调用 `get_capabilities` 获取紧凑能力摘要（< 3000 字符；需要契约详情时传 `include_schemas`）。
2. 已有项目先 `get_project_status`；新项目用 `create_project` 选择用户在 `<workspace>/profiles/<id>.yaml` 定义的 Profile（Agent 不可覆盖 Profile 规则）。
3. 每次进入新阶段可调用 `get_task_guidance` 或直接按写工具返回的 `next_action` 推进。
4. 在 `DAF_ALLOWED_SOURCE_ROOTS` 允许目录内 `ingest_sources`，通过 `find_sources` + `read_source_block` 阅读原文，再用 `register_evidence` 登记精确 `quote_excerpt`（L3 默认 `unassessed`，仅用户可标 `trusted`）。
5. `submit_outline` 提交大纲 → `request_approval(artifact_type="outline")` → 等待用户在本地 `daf ui` 或 `daf approvals decide` 审批。**严禁代用户运行审批命令。**
6. `get_section_context` → `submit_section`：
   - 事实性数据/比较句必须使用 Pandoc 原生内联引用 `[@ev_01]` / `[@ev_01; @ev_02]`；
   - 研究假设句末加 `[@hypothesis]` 且不得使用“实验表明/结果显示”等伪实验表述；
   - 传入 `author_session` 标识当前写作会话。
7. 审稿必须由宿主启动的**独立子 Agent 或新会话**完成，不读取作者写作过程：
   - `start_review(reviewer_id=<独立会话ID>)` 建任务并领取（与 `author_session` 相同会触发 `REVIEWER_IS_AUTHOR`）；
   - `submit_review` 逐项提交 `checklist_results`（每项必须附带章节正文原句 `quotes`，`not_applicable` 免引用但理由 ≥ 20 字）、显式 `recommendation`、`findings` 与全量 `claim_evaluations`；合法 `approve` 报告提交即自动验收。
8. 需记录待用户确认的建议时调用 `record_agent_note`（不会修改用户约束或模板冲突确认）。
9. 终稿经用户在 `daf ui` / `daf approvals decide` 批准后，调用 `render_document(purpose="release")` 与 `export_document(purpose="release")` 输出最终 DOCX/PDF。
