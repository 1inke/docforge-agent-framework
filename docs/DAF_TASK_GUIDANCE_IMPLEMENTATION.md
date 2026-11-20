# DAF Task-Aware Guidance 实施与验收记录

日期：2026-11-08。对应用户提供的 `DAF_Task_Aware_Agent_Guidance_架构设计_20261108.md` v1.0。
本文记录实际代码；该设计文档中的外部项目、后续建议不自动视为现有能力。

后续 v1.4 已补齐导入边界、状态路由、显式审核契约与专业方法：
以 [四项修改实施记录](DAF_GUIDANCE_FOUR_CHANGES_IMPLEMENTATION.md) 的升级说明为准。
下文 v1.3 的验收数字保留为该次实施记录。

## 实施范围

- Phase A：八类指导、单一 Profile 规范源、确定性哈希、字符预算、CapabilityRegistry 和 MCP/CLI/HTTP 统一入口已实现。
- Phase B：持久来源策略、四个来源入口强制权限、证据绑定/审核/审批/发布复验、申请与可信确认分离、撤权级联已实现。
- Phase C：新任务完整逐项审核门禁、持久 recommendation 与阻断行为、指导失败恢复信息已实现。保持原 Context API 响应及 token_budget 兼容语义；没有新增强制 guidance 票据或自动注入整段 Prompt。
- Phase D：未完成两个真实外部 Agent 宿主的文书质量实验。本地 MCP 调用与自动化契约测试不能冒充真实宿主或独立模型审稿。

## 实际结构与契约

契约统一定义在 `src/daf/core/contracts.py`，不维护第二份 guidance 模型：
`ProjectSourcePolicy`、`SourcePolicyChangeRequest`、`TaskGuidanceContract`。
`src/daf/guidance/` 包含八类版本化模板、纯构造器和来源策略校验器。

`get_task_guidance(project_id, task_kind, section_id=None, max_guidance_chars=8000)`
支持 source_analysis、evidence_extraction、plan_outline、draft_section、revise_section、
review_section、global_review、release_preflight。章节任务必须使用真实 Profile / 大纲章节 ID。

返回包含当前阶段、Profile 规则、有效字数、用户决定、禁改范围、来源权限、
输出契约、ready/blocked/needs_input/stale、失败恢复入口与 validity。
review_section 同时列出当前任务权威 checklist_items / checklist_hash。
release_preflight 使用现有 release 守卫检查，并区分 preview 与 release。

哈希使用规范化 JSON，不包含生成时间或随机数，也不因无关 state_version 增量变化。
相关 Profile、决策、来源索引、策略、证据版本、章节正文和依赖改变会更新指导。
指导读取只加载 SQLite 状态；不修改业务状态或写审计，不调用 LLM、不读取来源文件。

字符预算按 `json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`
的字符数检查。预算不足返回 GUIDANCE_BUDGET_EXCEEDED 与 minimum_required_chars，
不裁剪强制规则。HTTP/MCP 外层协议包装及客户端显示格式不计入该契约预算。

## 来源策略与可信确认

新项目显式保存 revision=1 的 project_only。allowed_source_ids=None 表示本项目所有
已摄取材料；[] 表示没有来源获准。V1 的 Discover/Read/Cite 共用允许集合，
校验器保留三项操作以便以后细分。外部来源 ID 仍需先摄取到本项目。

旧项目没有 source_policy 字段时，以 legacy_unconfigured / revision=0 视图读取：
兼容现有本地材料、不授权外联、不在只读请求中偷偷迁移。首次可信确认后落盘明确策略。
策略复用 ProjectWorkspaceState 的权威 SQLite JSON 与既有 CAS 事务，不引入第二份状态库。

`get_source_policy` 查看有效权限及请求。
`propose_source_policy_change(project_id, proposed_policy, reason)` 仅创建申请，不改变权限。
`trusted_decide_source_policy_change` 不在 Agent 工具白名单或 MCP 工具中。

可信确认方式：

- HTTP：`POST /api/v1/human-portal/projects/{project_id}/source-policy/{request_id}/decide`，
  body 为 decision / decided_by，header 为 `X-DAF-Human-Signature`。
- CLI：`daf confirm-source-policy --project-id ... --request-id ... --decision approved --portal-signature ...`。
- 可信宿主可调用 `compute_source_policy_signature(secret, project_id, request, decision, decided_by)`。
  签名绑定专用用途、项目、申请、提议内容、基准版本、决定和确认人；验证在 CAS 事务内。
  密钥至少 16 字符，通过可信服务的 DAF_HUMAN_PORTAL_SECRET 配置；不提供给 Agent。

CLI 与普通 HTTP 工具入口不天然可信。同一 OS 用户可读取进程环境或直接改工作区时，
HMAC 不能替代独立进程/账户/文件权限隔离。部署仍需满足已有人工确认域要求。

收紧策略后，已登记证据历史保留，相关绑定、章节及下游依赖、审稿和审批失效；
仅大纲已分配证据但尚未起草时，也撤销相关大纲/终稿批准与待处理申请。
受限证据不能重新绑定、审稿、审批或正式发布。放宽策略不无差别撤销既有正文/审批。
游标绑定来源集合、源索引与策略版本，不能换 source_ids 或撤权后继续复用。
Context 中的来源/证据也按策略过滤，导出账本只包含可读证据。

当前没有外部搜索连接器。approved_external / open_research 只表达授权意图，
实际 external_lookup_available=False，状态为 external_lookup_unavailable。
DAF 无法限制 Agent 通过宿主其他工具联网，也不撤回用户已拿到的历史文件或已读信息。

## 审稿门禁

`review_document` 新建任务保存 requires_structured_checklist=True；调用方传 false
不能降低任务要求。提交必须带当前 checklist_hash 和完整且不重复的逐项结果。
题目、ID、理由和来源权限会被检查；接受时再次校验清单与 verdict。

recommendation=approve/revise/block 持久化在报告中。
revise/block 不能被 accept；缺少对应 blocker/major findings 时自动建立修订问题。
历史任务缺此字段时保留旧兼容方式；历史 legacy_unstructured 报告可读，
不会被自动标为 structured_verified。新任务不能借历史默认路径绕过门禁。

Skill 精简为路由指南，search_sources 的模式由共享能力常量约束为 literal；
文体参考只讲写作方法，不另建章节 ID。保留原有 L1/L2a/L2b/L3、人工审批、
context_fingerprint、幂等、CAS、RenderJob 与 Word 公式模型。

## 使用示例

在 daf 项目目录执行：

```powershell
uv run daf --workspace C:/DAFData get-task-guidance --project-id demo --task-kind plan_outline
uv run daf --workspace C:/DAFData call get_task_guidance --params '@draft-guidance.json'
```

`draft-guidance.json` 示例（section_id 应替换为项目实际章节 ID）：

```json
{"project_id":"demo","task_kind":"draft_section","section_id":"sec_rationale"}
```

MCP 使用同名 get_task_guidance 工具；HTTP 使用
`POST /api/v1/tools/get_task_guidance`，body 为 `{"params": {...}}`。
未知任务、缺章节、预算不足、来源越权、签名缺失与策略冲突均返回可识别错误。

## 验证与剩余工作

基线 `uv run pytest -q`：100 passed（63.35 秒）。
最终运行结果：

```text
uv run pytest -q --tb=short
150 passed in 223.04s (0:03:43)

uv run pytest -q tests/test_daf_task_guidance.py --tb=short
50 passed in 25.48s

uv run ruff check src/daf/guidance tests/test_daf_task_guidance.py
All checks passed!

uv run ruff check src/daf --select E9,F63,F7,F82
All checks passed!

git diff --check
无差异格式错误
```

对修改的既有 Python 文件逐项比较 HEAD 与当前 Ruff 诊断，新增问题为 0。
既有代码仍存在完整 Ruff 规则下的历史问题，不将上述专项检查称作全仓完整 Ruff 通过。

新增专项测试覆盖四类 Profile × 八类任务、只读与确定性、三入口一致性、
预算、注入原文边界、空白名单、直接切片访问、证据登记、游标范围、
签名伪造/重放、可信 HTTP、旧状态、撤权、权限放宽以及新审稿门禁。
旧流程测试的审稿提交已补全显式逐项结果，没有通过降低新门禁兼容测试。
所有测试使用 tmp_path；未使用或修改用户 .daf_workspace 项目。

仍需外部验收：两个真实宿主分别处理科研申报书与专利交底书，保存阶段工具日志、
事实质量与人工返工记录；评估宿主 DAF 之外的工具权限。
原设计列出的活跃 RenderJob 回收风险与人工确认域部署检查仍属于独立上线事项，
本轮不宣称修复。尚未增加联网 Adapter、向量检索、多 Agent 编排器或策略编辑前端。

Git 发布范围：仅包含本次 DAF 源码、测试和文档；不包含本地记忆、运行数据或用户材料。
