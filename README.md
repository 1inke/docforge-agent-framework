# DocForge Agent Framework (DAF)

**面向外部 AI Agent（Codex / Antigravity / Claude Code / OpenCode / Hermes）的可发现、带状态、强契约专业长文与申报书执行框架。**

---

## 1. 核心设计理念

`DocForge Agent Framework (DAF)` 是一个**由外部 Agent 驱动、由框架强约束执行**的专业文书生产基础设施：

- **外部 Agent 负责（思考与创作）**：
  理解用户意图、提炼源材料证据、规划全篇大纲、按节撰写学术/工程正文、执行深度语义审校、与用户对话确认门禁。
- **DAF 框架负责（状态、契约与编译）**：
  1. **自我介绍能力（Capability Discovery）**：通过 [`agent_skill/SKILL.md`](agent_skill/SKILL.md) 与 `get_capabilities` / `get_document_profile` 动态暴露支持的文体规范、JSON Schema 与环境能力。
  2. **按需裁剪上下文、源切片分窗检索与全依赖新鲜度校验（Context-as-a-Service, Source Retrieval & Dependency Snapshot，DAF-P0-01, R3-02, R3-03, R3-04 & R3-05）**：通过 `list_source_blocks` / `search_sources` / `get_source_block` 提供全量源文档切片游标检索与滑窗读取（含 `expected_sha256` 与 `source_index_revision` 校验）；区分 `plan_outline` 与 `global_review` 专用项目级上下文构造器并提供 `get_section_content` 章节正文分窗读取；通过 `resolve_section_word_budget` 保证章节字数预算优先继承 Profile `SectionRule`；通过 `max_context_chars` 执行全响应 UTF-8 序列化字符硬上限裁剪，并计算包含有效字数预算与审核清单定义的任务隔离 `context_fingerprint`（`CONTEXT_STALE` / `CONTEXT_BUDGET_EXCEEDED`）。
  3. **不可绕过的人类审批信任域分离与预览/终稿分流（Approval Domain Separation，DAF-P0-02 & R2-P0-01）**：将非特权 Agent 调用域（仅暴露 `request_approval` / `get_approval_status`，绝不向 Agent 返回或落盘明文 `confirmation_token`）与受信任人类确认域（`decide_approval_by_human` / HMAC 签名门户确认）彻底分离；禁止通过 `profile_overrides` 关闭必选审批门禁；严格区分 `purpose="preview"` 与 `purpose="release"`。
  4. **SQLite 单一权威状态源、Create-Only 项目初始化与原子写事务（Atomic Transactions & CAS，DAF-P0-03 & R2-P1-04）**：基于 SQLite `BEGIN IMMEDIATE` 悲观写锁与 `state_version` 乐观 CAS 推进，`create_project` 采用严格 Create-Only 语义（重复 `project_id` 抛出 `PROJECT_ALREADY_EXISTS`，绝不覆盖历史数据）；JSON 快照降级为事务提交后的派生只读视图并支持自动对账修复（`repair_derived_snapshots`）。
  5. **三层证据核查链、L2a/L2b 双阶段支撑审校与版本级联失效（Evidence Versioning & L2a/L2b Claim Verification，DAF-P1-05, R2-P0-03 & R2-P1-06）**：构建 `L1 excerpt_match_status`（原文子串存在性）、`L2a rule_check_status`（严格针对 `quote_excerpt` 的确定性数值/极性校验，禁止信任未出处的 `metrics_data`）+ `L2b semantic_support_status`（语义支撑审校，禁止仅凭规则通过自封 `supported`）与 `L3 source_reliability`；`EvidenceItem` 具备版本演化记录（`evidence_revision` / `evidence_fingerprint`），重注册变更自动级联失效绑定主张、依赖章节、语义审校与下游审批（`EVIDENCE_CHANGED`）。
  6. **真实语义审核任务状态机与逐项 Checklist 核验（Semantic Review Lifecycle & Checklist Verification，R2-P0-02 & R3-01）**：`review_document` 创建携带结构化 `checklist_items`（`:q01..`）与 `checklist_hash` 的 `pending` 语义审核任务（`completed_at=None`），必须经由 `claim_semantic_review_task` -> `submit_semantic_review`（强制校验全部必填 `ChecklistItemResult`、理由非空与 `checklist_hash`，未通过项自动转 `ReviewIssue` 并阻断验收 `REVIEW_CHECKLIST_INCOMPLETE`）-> `accept_semantic_review` 完成并与章节当前 `revision` / `content_hash` / `context_fingerprint` 一致后方可放行 `final_draft` 审批与 `release` 渲染导出（`SEMANTIC_REVIEW_REQUIRED` / `SEMANTIC_REVIEW_STALE`）。
  7. **统一文档中间表示（DocumentIR）、输出路径安全边界与四阶段可恢复渲染（DAF-P0-04, DAF-P1-06 & R2-P1-05）**：在 `builtin`、`tagged`、`mapped` 三种模板模式下统一编译正文、原生 OMML 公式（`<m:oMath>`）、表格、列表与图注；严格校验 `output_filename`（拦截 `..`、盘符、ADS、保留设备名与符号链接逃逸 `ARTIFACT_PATH_INVALID`）；采用 `Prepare -> Build & Promote（锁外执行 Pandoc/DOCX I/O）-> Revalidate & Finalize -> Recover` 四阶段 `RenderJob` 消除长事务阻塞并保证崩溃恢复一致性。

---

## Task-Aware Guidance（协议 v1.4）

外部 Agent 可用 `get_task_guidance` 获取八类任务的当前指导，规则直接来自项目
Profile、来源策略、用户决定和工作流。MCP、CLI、HTTP 使用同一契约，查询不写状态。

```powershell
uv run daf --workspace C:/DAFData get-task-guidance --project-id demo --task-kind plan_outline
```

新项目默认 `project_only`；来源读取、证据绑定与发布由后端检查权限。
Agent 使用 `propose_source_policy_change` 申请授权，可信人工签名确认后才改变权限。
当前没有外部检索连接器。新审稿任务必须提交完整逐项 Checklist；revise/block 结论会持久阻断接受。

接口、兼容方式、签名确认和实际验收范围见 [实施记录](docs/DAF_TASK_GUIDANCE_IMPLEMENTATION.md)。

v1.4 已补齐默认拒绝导入、状态感知下一步、显式审稿结论与四类文书专业方法。
升级部署必须先配置批准的材料目录（JSON 数组，目录必须已经存在）：

```powershell
$env:DAF_ALLOWED_SOURCE_ROOTS = '["C:/DAFData/sources"]'
```

未配置和空数组都拒绝本地来源/模板导入。Guidance 新增 `task_status`、`next_action`；
新审稿必须显式提交 `recommendation`、`findings`，接受时核对事实性 Claim 的完整评价。
详见[四项修改记录](docs/DAF_GUIDANCE_FOUR_CHANGES_IMPLEMENTATION.md)。

## 2. 目录结构

```text
daf/
├── pyproject.toml                  # 独立项目配置（daf 与 daf-mcp 入口）
├── README.md                       # 项目说明与快速接入指南
├── docs/
│   ├── DAF_DESIGN_SPEC.md                  # 完整项目设计说明书（v1.0）
│   ├── DAF_SIX_ISSUES_REMEDIATION_PLAN.md  # 第一轮六项核心问题修改建议与实施方案
│   ├── DAF_R2_REMEDIATION_PLAN.md          # 第二轮审核六项遗留问题修复方案
│   └── DAF_R3_REMEDIATION_PLAN.md          # 第三轮上下文与资料工作流问题修改方案
├── agent_skill/
│   └── SKILL.md                    # 供外部 Agent 发现与阅读的交互协议说明书
├── src/daf/
│   ├── core/
│   │   ├── contracts.py            # 核心契约、DependencySnapshot、RenderManifest、DocumentIR 模型
│   │   ├── errors.py               # 统一机器可读异常体系（DAFError 及子类）
│   │   ├── source_import.py        # 默认拒绝的本地导入目录边界与快照读取
│   │   └── engine.py               # 核心接口统一编排引擎与非特权 Agent 调用守卫 (DAFEngine)
│   ├── capability_registry/        # 能力自描述与环境探测 (CapabilityRegistry)
│   ├── document_profiles/          # 文体无关默认 Profile 与可扩展 DocumentProfileContract 注册器
│   ├── guidance/                   # Task-Aware Guidance：状态感知路由、来源策略与可扩展方法注册器
│   ├── project_store/              # SQLite 单一权威源、BEGIN IMMEDIATE 原子事务、路径边界校验与派生快照对账 (ProjectStore)
│   ├── context_manager/            # 多源文档切片、分窗检索、字数预算继承、字符预算裁剪、DependencySnapshot 与 L2a/L2b 主张核查
│   ├── workflow_engine/            # 状态机、上下文指纹校验、门禁守卫、语义审核放行守卫与审批失效联动
│   ├── review_manager/             # 确定性规则质检（L1/L2a）+ 语义审校任务生命周期与逐项 Checklist 核验
│   ├── document_engine/            # 统一 DocumentIR 编译器、三模式 OMML 注入、四阶段 RenderJob 与隔离进程 PDF 导出
│   └── interfaces/
│       ├── mcp_server/             # 标准 FastMCP Server (daf-mcp)
│       ├── cli/                    # JSON 命令行接口 (daf)
│       └── http_api/               # FastAPI HTTP 接口、人类审批门户端点与状态探查服务
└── tests/
    ├── conftest.py                 # 临时目录导入边界测试夹具
    └── test_daf_core_framework.py  # 模板无关的框架核心能力、安全边界、状态机、渲染与三端接口验收测试
```

---

## 3. 快速开始与接入方式

> 本仓库仅包含文体与模板无关的 DAF 核心框架逻辑，不包含任何特定行业模板或预置文书格式。
> 可直接使用中性 `default` Profile 搭配 `profile_overrides`，或通过 `register_document_profile()` 注册自定义 `DocumentProfileContract`。

### 3.1 运行测试套件

```bash
uv run pytest -v
```

### 3.2 方式一：作为 MCP Server 接入（推荐 Antigravity / Claude Code / Cursor）

在外部 Agent 的 MCP 配置中添加：

```json
{
  "mcpServers": {
    "docforge-daf": {
      "command": "uv",
      "args": ["--directory", "<path-to-docforge-agent-framework>", "run", "daf-mcp"],
      "env": {
        "DAF_WORKSPACE_ROOT": "<path-to-workspace>",
        "DAF_ALLOWED_SOURCE_ROOTS": "[\"<path-to-approved-sources>\"]"
      }
    }
  }
}
```

### 3.3 方式二：作为 CLI 工具供终端型 Agent 调用（Codex / OpenCode）

```bash
# 1. 探测框架能力与注册规范
uv run daf get-capabilities --profile-id default

# 2. 创建项目
uv run daf create-project --project-id demo_project_01 --title "Example Document" --profile-id default --domain "general"

# 3. 通用 JSON 工具调用
uv run daf call get_project_status --params "{\"project_id\": \"demo_project_01\"}"
```

### 3.4 方式三：Python 核心库直接调用

```python
from daf import DAFEngine

engine = DAFEngine(workspace_root="./my_workspace", allowed_source_roots=["./sources"])
caps = engine.get_capabilities(profile_id="default")
```
