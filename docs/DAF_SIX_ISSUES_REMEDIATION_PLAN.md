# DocForge Agent Framework（DAF）六项核心问题修改建议与实施方案

> **文档性质**：工程修改建议 / 实施设计 / 验收基线  
> **对应项目**：`1inke/docforge` 仓库内独立派生子项目 `daf/`（不是旧版 `app/` 或 `docforge-new/`）  
> **审查基线**：`main` 分支提交 `cde5e4d27cd7bfd9ff8f8b5ef44bb5f1c924970a`（2026-11-07）  
> **文档版本**：v1.0  
> **目标**：修复先前代码审查指出的六项问题，形成可实施、可测试、可回归的变更方案。  
> **说明**：本文是修改建议，不代表代码已修复；代码位置与问题描述以审查基线为准。

---

## 0. 修改原则与总体优先级

### 0.1 不改变核心产品定位

DAF 是**外部 Agent 驱动的专业文书执行框架**：Agent 负责理解任务、规划、写作、专业语义审查及与用户沟通；DAF 负责契约、项目状态、规范、可信上下文、版本化提交、确定性校验及 DOCX/PDF 交付。

本轮不引入内置大模型路由作为必需路径，不重做前端，不重构已经清晰的六大模块边界。优先修复**一致性、放行安全、并发正确性、证据可靠性、文档可复现性**。

### 0.2 六项问题清单

| 编号 | 优先级 | 问题 | 核心验收目标 |
|---|---|---|---|
| DAF-P0-01 | P0 | 上下文版本生成了但提交时未校验 | 旧上下文不能提交为新版本 |
| DAF-P0-02 | P0 | 审批不是正式流程的强制门禁 | 未经有效人工批准不能正式发布 |
| DAF-P0-03 | P0 | SQLite 整体状态读写并非原子 CAS | 并发提交不能发生丢失更新；幂等与审计一致 |
| DAF-P0-04 | P0 | DOCX 导出可能复用旧文件 | 导出内容必须与已确认的最新项目快照一致 |
| DAF-P1-05 | P1 | 证据核验只确认摘录存在 | 区分摘录匹配、主张支持、来源可信度 |
| DAF-P1-06 | P1 | 三种 DOCX 模式语义与渲染不一致 | 同一结构化内容在三种模式下不静默降级或错位 |

**建议实际实施顺序**：P0-03 → P0-01 → P0-02 → P0-04 → P1-05 → P1-06。P0-03 的持久化事务底座先做，其他修改可依赖统一的版本控制与原子提交。

---

## 1. DAF-P0-01：上下文版本校验与失效机制

### 1.1 当前问题

涉及文件：

- `daf/src/daf/context_manager/manager.py`：`build_section_context()` 生成 `context_revision`。
- `daf/src/daf/core/contracts.py`：`SectionContextContract`、`SectionSubmissionContract`。
- `daf/src/daf/workflow_engine/state_machine.py`：`WorkflowEngine.submit_section()`。
- `daf/src/daf/core/engine.py`：`DAFEngine.get_section_context()`、`submit_section()`。

当前上下文版本主要由**章节修订号、大纲修订号、证据 ID、问题 ID、决策数量**等组成；提交章节时只检查 `base_revision`，并未验证提交所携带的 `context_revision`。因此即使依赖的证据正文、上游内容或约束已变化，只要目标章节的修订号没变，陈旧上下文仍可能被接受。

### 1.2 建议设计：基于依赖快照的上下文指纹

将上下文分为两类：

1. **内容指纹（`context_fingerprint`）**：反映当前章节所依赖的实际业务事实和约束。
2. **呈现预算（`token_budget`）**：决定本次返回多少内容，不应单独改变业务上下文是否过期的判定。

新增或明确以下字段：

```json
{
  "project_id": "demo",
  "section_id": "sec_methods",
  "current_section_revision": 2,
  "context_revision": "ctx_v2_88f3...",
  "context_fingerprint": "sha256:88f3...",
  "dependency_snapshot": {
    "outline_revision": 3,
    "outline_hash": "...",
    "profile_version_hash": "...",
    "upstream_sections": {"sec_background": "sha256:..."},
    "evidence": {"E014": "sha256:..."},
    "decision_constraints_hash": "...",
    "review_issues_hash": "..."
  },
  "truncated": false
}
```

指纹的最小输入应包括：当前章节契约与大纲节点、所需上游章节的**内容哈希**、所选证据的**摘录内容及验证状态**、来源版本、术语表、已生效用户约束、修改禁区、影响本节的开放审核问题和文书规范版本。使用排序后的规范化 JSON 计算 SHA-256。不要仅对证据 ID 或决策条目数量取哈希。

> **细节**：不能用“整个项目更新时间”判定上下文失效，否则修改无关章节也会导致全篇上下文全部过期。应以**任务相关依赖**计算失效范围。

### 1.3 提交时校验

`submit_section()` 执行顺序：

1. 在事务内读取当前项目及目标章节版本。
2. 校验 `base_revision == 当前章节 revision`。
3. 以**相同的确定性算法**重新计算该章节的当前 `context_fingerprint`。
4. 校验 `submission.context_revision` 与当前指纹匹配。
5. 通过后才进行证据绑定校验及状态提交。

如果不一致，返回明确的业务错误：

```json
{
  "error_code": "CONTEXT_STALE",
  "message": "Section context changed after it was retrieved.",
  "section_id": "sec_methods",
  "submitted_context_revision": "ctx_v2_old",
  "current_context_revision": "ctx_v2_new",
  "suggested_action": "Call get_section_context and regenerate or merge your draft."
}
```

对 `CONTEXT_STALE` 不应自动覆盖或静默重试，否则会掩盖“新约束未被写作模型考虑”的问题。Agent 必须重新获取上下文；是否需要重写全文，由 Agent 基于差异判断。

### 1.4 与 Agent 工作流的关系

- Agent 调用 `get_section_context()`，接收正文任务与上下文版本。
- Agent 生成正文后提交 `base_revision`、`context_revision`。
- DAF 对当前依赖指纹做一致性校验。
- 过期则返回新版本与变更原因，Agent 再决定局部重写或合并。

可选增加 `get_context_diff(project_id, section_id, previous_context_revision)`，先不作为第一阶段必需接口。

### 1.5 验收测试

- **T-CXT-01**：领取第三章上下文后更新第二章（第三章依赖第二章），提交第三章旧上下文，必须返回 `CONTEXT_STALE`。
- **T-CXT-02**：领取上下文后修改本节的用户约束，旧上下文必须失效。
- **T-CXT-03**：证据摘录或验证状态变化，引用该证据的章节上下文失效。
- **T-CXT-04**：只修改不相关章节，原章节上下文不应无故失效。
- **T-CXT-05**：上下文被截断时仍有稳定、可检查的依赖指纹；不因为不同 `token_budget` 产生无意义的过期判定。

---

## 2. DAF-P0-02：审批门禁和真实人工确认

### 2.1 当前问题

涉及文件：

- `daf/src/daf/core/engine.py`：`render_document()`、`export_document()` 的 `require_final_approval=False`。
- `daf/src/daf/workflow_engine/state_machine.py`：`approve_artifact()`、`submit_section()`。
- `daf/src/daf/interfaces/mcp_server/server.py`：对外暴露审批和导出接口。

当前 `render_document` / `export_document` 默认不要求最终审批，`submit_section` 也没有将“已批准大纲”作为硬约束。`approved_by="user"` 是调用方传入的普通字符串，本身不能证明用户确实进行了交互确认。

### 2.2 建议设计：预览与正式发布分离

建议将文档操作显式划分为：

| 操作 | 行为 | 是否允许未审批 |
|---|---|---|
| `render_document(purpose="preview")` | 内部预览，用明显的草稿标识 | 可允许 |
| `render_document(purpose="release")` | 生成正式可发布的 DOCX | 不允许 |
| `export_document(purpose="preview")` | 调试／审阅包，注明未批准 | 可允许 |
| `export_document(purpose="release")` | 正式交付包 | 不允许 |

可保留现有接口名以减少破坏性更新，但要删除依赖可选布尔参数决定关键门禁的设计；至少确保对外默认是严格模式，并明确预览输出不可被标记为正式成品。

### 2.3 正式发布的必要条件

建议提取统一函数 `assert_release_ready(state, expected_manifest_hash)`，至少检查：

1. 当前大纲版本已经有效审批；
2. 所有必需章节存在、内容非空；
3. 当前章节与所依赖的上游章节不处于 `needs_revalidation`；
4. 必要语义审核已完成并对应当前版本（后续审核模块补齐）；
5. 不存在未解决的阻断级问题；重大问题是否阻断由 Profile 明确配置；
6. `final_draft` 审批绑定的聚合哈希与当前内容快照一致；
7. 模板与规范的关键冲突已经获得用户确认。

### 2.4 区分“Agent 声明”与“用户授权”

不应仅因为 Agent 调用 `approve_artifact(approved_by="user")` 就视为用户确认。建议引入审批来源：

```json
{
  "approval_id": "appr_005",
  "artifact_type": "final_draft",
  "artifact_hash": "sha256:...",
  "confirmed_via": "trusted_ui_or_local_confirmation",
  "confirmed_by": "human_user",
  "confirmed_at": "..."
}
```

可选实施路径：

- **本地单用户**：通过可见的本地确认界面或独立确认命令完成，Agent 仅负责提出审批请求。
- **多客户端／远程**：通过经身份验证的审批通道完成，令牌必须绑定项目、工件哈希和有效期，使用一次后失效。
- **自动测试**：允许专用测试凭证，但不得混入生产批准记录。

### 2.5 门禁状态与失效

必须保留“批准的是特定版本”的原则。任一受保护内容修改后自动失效；正式发布检查必须重新计算当前聚合哈希，而不能只读 `status="approved"`。

同时明确：对 Agent 提交和审核的正常工作允许持续进行；对**正式发布**进行强制拦截，避免把工作流设计成难以使用的全锁定模式。

### 2.6 验收测试

- **T-APR-01**：未批准大纲，正式写作提交应被阻止（如果产品允许预备草稿，必须使用明确的 `draft_preapproval` 模式）。
- **T-APR-02**：未批准最终正文，`release` 渲染、导出均失败。
- **T-APR-03**：手动传入 `approved_by="user"` 不能绕过真实确认流程。
- **T-APR-04**：章节修订后，旧最终审批失效；旧批准哈希不能用于发布。
- **T-APR-05**：预览可以生成，但输出元数据、文件名或水印明显标注草稿性质。

---

## 3. DAF-P0-03：SQLite 原子并发控制与幂等性

### 3.1 当前问题

涉及文件：

- `daf/src/daf/project_store/store.py`：`save_state()`、`load_state()`、幂等记录、审计记录。
- `daf/src/daf/core/engine.py`：`submit_section()` 的“查幂等 → 载入 → 修改 → 保存 → 写幂等 → 记审计”。

目前使用 SQLite，但整个 `state_json` 采用整体读写。两个并发调用可能基于相同旧修订号分别通过内存校验；后写入者覆盖先写入者。并且项目状态、幂等响应、审计分别提交，异常发生时可留下不完整结果。

### 3.2 建议的最小可靠方案

第一阶段可以继续使用整体 JSON 状态模型，不必立即拆成完全关系化的 `sections` 表；但需要建立数据库层面的**项目版本 CAS**。

在 `projects` 表增加：

```sql
ALTER TABLE projects ADD COLUMN state_version INTEGER NOT NULL DEFAULT 0;
```

所有写操作改用统一的事务执行器：

```python
# 伪代码：必须在同一个 SQLite 事务中执行
with project_store.write_transaction(project_id) as tx:
    state, version = tx.load_for_update()
    tx.assert_idempotency_key_unused_or_same_payload(key, request_hash)
    result = business_operation(state)  # 版本、上下文、证据、审批等校验
    tx.save_state_if_version_matches(
        expected_version=version,
        new_version=version + 1,
        state=state,
    )
    tx.save_idempotency_record(key, request_hash, result)
    tx.append_audit_event(...)
# 提交成功后，再异步/同步刷新便于人工阅读的 JSON 快照
```

SQLite 可使用 `BEGIN IMMEDIATE` 序列化同一个数据库文件中的写入者，并在提交时通过 `UPDATE ... WHERE state_version = ?` 做最终版本保护。对跨进程并发不能只用 Python `threading.Lock`。

### 3.3 幂等键必须绑定请求摘要

当前 `get_idempotent_response(project_id, key)` 不验证当前请求正文与最初请求是否一致，同一 `idempotency_key` 可以对不同 payload 返回旧结果。

建议增加 `request_hash` 字段；判定规则：

- 相同键、相同 payload：返回第一次成功结果（`idempotent_replay=true`）。
- 相同键、不同 payload：返回 `IDEMPOTENCY_KEY_REUSED`，不可返回旧结果。
- 新键、旧 `base_revision`：返回 `REVISION_CONFLICT`。

**注意**：应对请求的语义字段做规范化 JSON 摘要，排序键并使用稳定编码，避免 JSON 字段顺序造成误判。

### 3.4 数据库与文件快照的权威关系

建议规定：

- **SQLite 是唯一权威状态源**；
- `state_snapshot.json`、各子目录 JSON、`audit_events.jsonl` 是派生可读快照；
- 只有数据库事务成功后，才刷新这些文件；
- 派生文件失败时记录待修复任务，下次可以由数据库重建，不能反向覆盖新数据库状态。

同一工件的渲染文件不应写入数据库事务中；应使用“准备临时文件 → 校验 → 原子移动 → 写入渲染清单”的方式与事务配合。

### 3.5 需要改动的结构

| 组件 | 修改方向 |
|---|---|
| `ProjectStore` | 新增统一 `transact(project_id, operation, idem...)`、项目版本 CAS、单事务审计 |
| `DAFEngine` | 所有状态变更走事务执行器，避免自由调用 `load_state()` 后 `save_state()` |
| `idempotency_records` | 增加 `request_hash`，保存第一次成功响应及结果版本 |
| 快照输出 | 改为数据库提交后的派生同步，失败可重建 |
| 数据迁移 | 使用 `PRAGMA user_version` 或专门迁移脚本保证旧工作区升级 |

### 3.6 验收测试

- **T-TXN-01**：两个线程或进程同时提交同一章节修订，一个成功、另一个明确冲突，不丢失提交。
- **T-TXN-02**：并发修改不同章节时不得用旧整体 JSON 覆盖其他章节更新。
- **T-TXN-03**：模拟写入中断，不得出现“状态更新、幂等记录缺失”的半提交。
- **T-TXN-04**：相同幂等键与相同 payload 重放不产生新修订号；相同键与不同 payload 必须报错。
- **T-TXN-05**：数据库与 JSON 快照不一致时，以数据库为准并能恢复派生快照。

---

## 4. DAF-P0-04：渲染快照绑定与防止过期导出

### 4.1 当前问题

涉及文件：

- `daf/src/daf/core/engine.py`：`export_document()`。
- `daf/src/daf/document_engine/docx_builder.py`：`render_document()`。
- `daf/src/daf/core/contracts.py`：`ProjectWorkspaceState.last_rendered_docx_path`。

目前只要 `last_rendered_docx_path` 存在，导出可能直接复用该文件，没有判断它对应的正文版本、模板版本和格式规则是否仍有效。

### 4.2 建议建立 Render Manifest

每次成功渲染后保存一份结构化清单：

```json
{
  "render_id": "render_20261107_001",
  "purpose": "release",
  "input_fingerprint": "sha256:...",
  "document_content_hash": "sha256:...",
  "section_hashes": {
    "sec_background": "sha256:...",
    "sec_methods": "sha256:..."
  },
  "outline_hash": "sha256:...",
  "profile_hash": "sha256:...",
  "template_sha256": "sha256:...",
  "section_mapping_hash": "sha256:...",
  "renderer_version": "daf-docx-v2",
  "docx_path": "artifacts/demo_compiled.docx",
  "docx_sha256": "sha256:...",
  "created_at": "..."
}
```

`input_fingerprint` 由**最终有序章节内容及结构化块、标题与大纲顺序、排版规则、模板 SHA、字段映射及渲染引擎版本**计算得到。

不能仅使用章节修订号求和：不同修改序列有可能得到相同总和；必须使用有序、规范化的内容快照。

### 4.3 导出前的完整校验

`export_document()` 应执行：

1. 重新计算当前项目的 `input_fingerprint`。
2. 对比最新 Render Manifest 的 `input_fingerprint`。
3. 对比实际 DOCX 文件 SHA 与清单中的 `docx_sha256`。
4. 对正式发布再次执行当前版本的 `assert_release_ready()`。
5. 有任一不一致：重新渲染或返回 `RENDER_STALE`（建议正式流程自动重新渲染并记录原因）。
6. PDF 必须由这份刚校验过的 DOCX 导出；导出包记录 `source_docx_sha256`。

### 4.4 文件生成的原子性

先写入受控 `artifacts/.tmp/`，验证 DOCX 可打开、结构检查通过后，再执行原子替换到目标文件名；写入渲染清单后才更新“最新有效渲染”的状态。失败时保留前一成功工件，但必须将其标记为**历史版本**，不能作为当前正式交付物。

### 4.5 验收测试

- **T-REN-01**：渲染 r1 → 修改正文为 r2 → 直接导出，结果必须包含 r2 内容。
- **T-REN-02**：正文没变但排版 Profile 改变，旧 DOCX 不得复用。
- **T-REN-03**：替换模板或调整映射，旧 DOCX 不得复用。
- **T-REN-04**：外部手动修改已渲染 DOCX，导出前能检测 SHA 不匹配。
- **T-REN-05**：PDF 元数据对应当前 DOCX SHA；失败时不会把旧 PDF 当成新输出。

---

## 5. DAF-P1-05：建立分层证据验证与主张支持审查

### 5.1 当前问题

涉及文件：

- `daf/src/daf/context_manager/manager.py`：`verify_and_build_evidence()`。
- `daf/src/daf/core/contracts.py`：`EvidenceItem`、`SourceBinding`。
- `daf/src/daf/workflow_engine/state_machine.py`：证据绑定校验。
- `daf/src/daf/review_manager/reviewer.py`：证据覆盖质量检查。

当前 `verification_status="verified"` 代表规范化摘录确实出现在源切片中。它并不能证明原始资料可信，更不能证明 `claim_summary` 或 `SourceBinding.claim_text` 得到摘录支持。尤其应避免将“字符串存在”与“科学论据已核实”混为一谈。

### 5.2 三层语义必须分开

| 层次 | 验证对象 | 执行主体 | 建议状态 |
|---|---|---|---|
| L1 摘录完整性 | 引用文本是否出现在指定原始切片中 | DAF 确定性校验 | `matched / not_matched` |
| L2 主张支持性 | 摘录是否真的支持该章节的具体论断 | 外部 Agent 审核，必要时人工确认 | `supported / partially_supported / unsupported / pending` |
| L3 来源可靠性 | 是否权威、是否过期、是否存在方法学限制或引用风险 | Agent / 人工 | `trusted / conditional / unassessed / rejected` |

为了兼容原有代码，可以暂时保留 `verification_status`，但必须在 Schema 文档中明确它只表示 L1；更推荐新增清晰的 `excerpt_match_status`，逐步淘汰含糊的 `verified` 名称。

### 5.3 增加 Claim–Evidence 绑定记录

将单纯的 `SourceBinding(claim_text, evidence_id)` 扩展为：

```json
{
  "binding_id": "B024",
  "section_id": "sec_methods",
  "section_revision": 3,
  "claim_text": "该机制能够降低推理时延",
  "evidence_id": "E014",
  "evidence_quote_hash": "sha256:...",
  "support_status": "pending",
  "support_rationale": "尚未核查是否存在直接对比实验",
  "reviewed_against_revision": null
}
```

支持性审查任务由 DAF 发给外部 Agent，Agent 返回判断、支持片段与理由；DAF 保存审核结果和来源版本。不同 Agent 的语义审核本身不等于客观事实真值，正式流程可按文书类型设定哪些高风险主张必须人工确认。

### 5.4 约束与风险控制

1. **不得用模型生成的引用片段替代原始摘录**；L1 校验必须针对不可变的源切片。
2. **不得让同一个空泛结论仅因绑定任一已核验摘录就通过**；L2 按具体 `claim_text` 判断。
3. **源文件更新后自动使相关证据过期**；来源 SHA 改变时，对下游绑定与审核结果进行级联重审。
4. **区分主张等级**：常识背景、待验证假说、已经完成的实验结论，对证据要求不同；不可要求每一句观点都附同一类型证据。
5. **禁止循环自证**：Agent 自己生成的“参考资料总结”不能自动变成独立权威来源。

### 5.5 验收测试

- **T-EVD-01**：原始摘录能匹配，但 `claim_text` 夸大结论时应被标为不支持或待审，而非默认通过。
- **T-EVD-02**：引用了正确来源但错误实验数值，应检出冲突。
- **T-EVD-03**：原文件版本变动后，引用它的证据审查状态必须失效。
- **T-EVD-04**：无数据时可以以“研究假设”写作，但不能被审核为“已验证实验结果”。
- **T-EVD-05**：交付包保留从主张 → 绑定 → 引文 → 源文件及定位的追溯链。

---

## 6. DAF-P1-06：统一 DOCX 中间表示与渲染一致性

### 6.1 当前问题

涉及文件：

- `daf/src/daf/document_engine/docx_builder.py`：`append_markdown_via_pandoc_or_docx()`、`render_document()`。
- `daf/src/daf/core/contracts.py`：`StructuredBlock`、`LayoutProfile`。
- `daf/src/daf/context_manager/manager.py`：模板探针。

当前三个输出路径处理方式不同：

- `builtin`：段落经 Pandoc 转换并插入文档，较好支持 OMML。
- `tagged`：docxtpl 把 Markdown 当作普通字符串注入模板，未保证原生公式、表格和列表一致。
- `mapped`：支持显式单元格定位，但 Pandoc 缺失进入纯 `python-docx` 回退分支时，回退逻辑使用 `target_doc.add_paragraph()`，不保证写入指定 `container_element`，存在错位风险。

### 6.2 推荐架构：一份 Document IR，三个渲染适配器

新增统一中间表示（Intermediate Representation，IR）：

```text
Agent 输出（Markdown + StructuredBlocks）
        ↓ 解析、标准化、验证
Document IR（文档树、段落、列表、表格、公式、图片引用、引文、样式语义）
        ├── BuiltinRenderer → 新建 DOCX
        ├── TaggedTemplateRenderer → 带标签的 DOCX
        └── MappedTemplateRenderer → 显式定位的 DOCX
        ↓
统一 OpenXML QA + 公式检查 + 内容完整性检查
```

推荐 IR 最小块类型：`heading`、`paragraph`、`list`、`table`、`equation`、`figure_ref`、`caption`、`page_break`。每个块可以携带 `block_id`、`section_id`、`style_role`、`source_refs`。

### 6.3 解决三种模式的具体办法

**BuiltinRenderer**

- 按 IR 顺序创建标题与正文；
- 公式统一编译为原生 OMML；
- 不重复插入相同 StructuredBlock 内容；
- 表格和图题采取统一编号、交叉引用策略。

**TaggedTemplateRenderer**

- 不再将包含复杂排版的 Markdown 直接当普通字符串写入 `{{ placeholder }}`；
- 可将占位符作为 XML 替换锚点，先在受控中间文档完成富文本/公式转换再插入；
- 必须同步处理 OpenXML 关系（图像、超链接、编号等），不能仅粗暴复制有外部 `r:id` 依赖的节点；
- 对当前不支持的富文本占位符返回明确错误，而不是悄悄丢公式或样式。

**MappedTemplateRenderer**

- 显式映射目标 `table:t:r:c` 保留；建议增加章节／书签锚点类型；
- 所有回退路径都必须向指定单元格或锚点插入，不得写到文档末尾；
- 映射目标不存在、重复或冲突时严格报错；
- 不覆盖原模板，渲染后对关键结构进行对比。

### 6.4 严格的降级策略

不得静默把数学公式转换成原始 LaTeX 文本并继续宣称“原生 OMML 渲染成功”。

建议：

- `strict`（正式交付默认）：缺少 Pandoc 或公式转换失败，返回可操作错误；
- `preview_plaintext`（仅调试）：允许降级，但响应和文件元信息必须标明公式未完成转换；
- 所有模式都应返回统一的结构化渲染报告。

### 6.5 统一质量检查

最终检查至少覆盖：

1. 必需章节是否齐全，顺序是否正确；
2. 所有预期的公式节点是否为 OMML；
3. 未替换模板占位符是否为零；
4. 映射区域内容是否存在，且没有误写到文档尾部；
5. 表格、标题、图片引用及分节是否保留；
6. 原始模板 SHA 没有变化；
7. 中英文字体、标题样式、页边距、行距是否符合 Profile / 用户模板规则；
8. 输出可以由标准 DOCX 解析器重新打开，必要时通过 PDF 视觉检查补充验证。

### 6.6 验收测试

- **T-DOC-01**：同一份正文的公式，在 `builtin`、`tagged`、`mapped` 模式下均为原生 OMML，或明确声明该模式不支持而拒绝正式发布。
- **T-DOC-02**：指定单元格且 Pandoc 不可用时，正文不能转而写入文档尾部。
- **T-DOC-03**：模板映射错误必须失败，而不是猜测其他位置。
- **T-DOC-04**：复杂表格、列表、公式和图题的顺序不变；无内容丢失。
- **T-DOC-05**：模板文件字节哈希不变；输出字体与结构规则通过检查。

---

## 7. 跨模块接口与数据契约改动总表

| 文件／模块 | 建议新增或调整 | 影响 |
|---|---|---|
| `core/contracts.py` | `ContextFingerprint` / `DependencySnapshot`、`RenderManifest`、明确的证据分层状态、审批凭证结构、`DocumentIR` | 需兼容已有 ProjectWorkspaceState 快照 |
| `context_manager/manager.py` | 生成规范化上下文依赖指纹、基于来源哈希的证据失效 | `get_section_context` 输出扩展 |
| `workflow_engine/state_machine.py` | 提交时检查上下文版本；审批放行检查；大纲/证据/依赖失效 | 旧版提交可能开始返回冲突错误 |
| `project_store/store.py` | 全部写操作在 SQLite 事务内执行，状态版本 CAS，幂等 payload hash | 需要数据库迁移 |
| `core/engine.py` | 状态写操作统一事务；正式/预览渲染分离；导出前检查清单 | MCP、CLI、HTTP 的返回值可能扩展 |
| `review_manager/reviewer.py` | 按主张和证据进行语义审核任务登记；记录基准版本 | 审核未完成时正式放行失败 |
| `document_engine/docx_builder.py` | 统一 IR 渲染，加入 RenderManifest 和跨模式检查 | 三种渲染模式受影响 |
| `interfaces/mcp_server/server.py` | 更新工具说明、Schema、错误码和人工确认路径 | 外部 Agent Skill 必须同步 |
| `agent_skill/SKILL.md` | 明确 `CONTEXT_STALE`、`REVISION_CONFLICT`、正式发布门禁和重新领取上下文流程 | 改善 Agent 自动恢复能力 |

### 7.1 错误码契约（建议）

```json
{
  "ok": false,
  "error": {
    "code": "CONTEXT_STALE",
    "message": "Context fingerprint has changed.",
    "retryable": true,
    "recommended_tool": "get_section_context",
    "details": {
      "project_id": "demo",
      "section_id": "sec_methods"
    }
  }
}
```

建议统一的业务错误：

- `REVISION_CONFLICT`：章节修订号发生冲突；
- `CONTEXT_STALE`：上下文依赖版本过期；
- `IDEMPOTENCY_KEY_REUSED`：相同幂等键携带不同请求；
- `APPROVAL_REQUIRED` / `APPROVAL_STALE`：缺少有效人工审批；
- `RENDER_STALE`：文件与当前项目快照不符；
- `EVIDENCE_UNVERIFIED` / `CLAIM_UNSUPPORTED`：证据或论断未达到规则要求；
- `TEMPLATE_MAPPING_INVALID` / `RENDER_DEPENDENCY_MISSING`：模板映射错误或缺少必需渲染组件。

MCP、CLI、HTTP 的上层包装可以不同，但内部业务码应保持一致。

---

## 8. 推荐开发任务拆分

### 阶段 A：持久化与一致性底座（P0-03）

- [ ] A1：增加状态版本与数据库迁移；
- [ ] A2：实现统一写事务和基于版本的 CAS；
- [ ] A3：将幂等记录、业务状态、审计记录合并为一次原子提交；
- [ ] A4：完善并发、崩溃恢复与重复请求测试。

**交付标准**：多 Agent 并发情况下无丢失更新，重复调用不会产生不可解释的状态。

### 阶段 B：上下文版本与审批（P0-01 / P0-02）

- [ ] B1：引入 ContextFingerprint 算法与版本 Schema；
- [ ] B2：提交章节时进行完整依赖校验；
- [ ] B3：设计 `preview` / `release` 语义及正式发布放行函数；
- [ ] B4：人工确认凭证不再由 Agent 自报身份替代；
- [ ] B5：明确旧版审批、上下文失效的迁移策略。

**交付标准**：对已改变依赖的旧任务提交和未经批准的正式发布，DAF 都会拒绝。

### 阶段 C：渲染工件可信性（P0-04）

- [ ] C1：设计 RenderManifest；
- [ ] C2：使用输入快照哈希判定是否可复用 DOCX；
- [ ] C3：PDF、DOCX、导出包绑定一致的版本和文件哈希；
- [ ] C4：加入文件损坏、旧渲染及中途失败测试。

**交付标准**：任何正式导出的文件均可回溯到最新通过审批的内容快照。

### 阶段 D：证据审查与文档 IR（P1-05 / P1-06）

- [ ] D1：区分摘录匹配、支持性和来源可信度；
- [ ] D2：建立审查结果与章节／证据版本的绑定；
- [ ] D3：引入统一中间文档结构；
- [ ] D4：统一三种模板渲染规则和失败策略；
- [ ] D5：增加复杂中文模板、公式、表格及跨模式回归测试。

**交付标准**：无主张—证据误认证；无静默格式降级；无模板错位写入。

---

## 9. 发布前综合验收场景

以一份需要**四章以上、两份以上资料、一个 Word 模板、两轮修改**的真实科研申报书为基准，执行以下完整用例：

1. 外部 Agent 通过 MCP/CLI 发现 Profile，创建项目，摄取资料并登记可追溯证据；
2. Agent 提交大纲，用户在可信通道确认具体版本；
3. Agent 分章节领取上下文并写作，不依赖聊天历史存储正式状态；
4. 在第三章写作期间修改第二章或用户约束，旧的第三章上下文必须被拒绝；
5. 两个 Agent 同时提交同一章节，最多一个成功，其余收到可解释的冲突；
6. 人工提出局部意见并限定“不得改变第一章”；修改目标章节后其他无关章节内容哈希保持不变；
7. 审查确认证据引用和语义审查任务均达到该文体的发布要求；
8. 用户确认最终正文；修改后的旧审批不能继续发布；
9. 分别测试新建 DOCX 与复杂模板映射输出，验证原生公式及模板结构；
10. 渲染后再次修改正文，直接导出必须重新渲染或拒绝旧文件；
11. 输出 DOCX、可选 PDF、审计记录与可追溯证据矩阵；
12. 新开 Agent 会话仍能依据 DAF 项目状态继续完成工作。

### 最终通过条件

- **一致性**：每一次正式提交可追溯到具体章节版本与上下文依赖快照；
- **并发可靠性**：无丢失更新或幂等假成功；
- **人审可信**：正式成稿与批准内容哈希一致，确认来源可核实；
- **证据严谨**：不将原文字符串匹配等同于科学结论成立；
- **交付可靠**：输出是当前已批准状态对应的文件，结构与公式满足目标 Profile；
- **跨 Agent 可恢复**：更换 Agent 或重启会话不会丢失项目约束、审核与修改历史。

---

## 10. 需要避免的实施误区

1. **不要把所有上下文失效都等同于全文重写**。应使用受影响的依赖图局部失效，减少重复推理成本。
2. **不要让“模型说已经审查通过”替代版本化的审核记录**。审核结论必须绑定输入版本及执行时间。
3. **不要只用 SQLite 替代 JSON 就宣称具备事务安全**。一致性关键在于读—校验—写是否处于一个并发控制闭环。
4. **不要仅根据“文件存在”判断可导出**。必须验证它匹配当前内容、模板和格式规则。
5. **不要将 `verified` 用作含糊的统一证据质量等级**。摘录匹配、论证支持和来源可信度是不同问题。
6. **不要为消除格式错误直接降级成纯文本并默认为成功**。正式 DOCX 需要严格可见的失败语义。
7. **不要把用户审批交给 Agent 自己伪装调用完成**。工作流的“人工确认”必须体现真实的人工授权边界。

---

## 附录：审查代码入口

- [`DAFEngine`](https://github.com/1inke/docforge/blob/main/daf/src/daf/core/engine.py)
- [`ContextManager`](https://github.com/1inke/docforge/blob/main/daf/src/daf/context_manager/manager.py)
- [`WorkflowEngine`](https://github.com/1inke/docforge/blob/main/daf/src/daf/workflow_engine/state_machine.py)
- [`ProjectStore`](https://github.com/1inke/docforge/blob/main/daf/src/daf/project_store/store.py)
- [`ReviewManager`](https://github.com/1inke/docforge/blob/main/daf/src/daf/review_manager/reviewer.py)
- [`DocumentEngine`](https://github.com/1inke/docforge/blob/main/daf/src/daf/document_engine/docx_builder.py)
- [`Pydantic Contracts`](https://github.com/1inke/docforge/blob/main/daf/src/daf/core/contracts.py)
- [`DAF MCP Server`](https://github.com/1inke/docforge/blob/main/daf/src/daf/interfaces/mcp_server/server.py)
- [`DAF Acceptance Tests`](https://github.com/1inke/docforge/blob/main/daf/tests/test_daf_contracts_and_workflow.py)

**建议在开始修改前创建独立分支，并为每一个编号（DAF-P0-01 等）建立单独的 issue/PR 和对应回归测试，避免多模块修改混杂后难以定位回归问题。**
