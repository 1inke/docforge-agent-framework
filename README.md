# DocForge Agent Framework (DAF)

**面向本地单用户场景的 AI Agent 专业文书写作护栏框架（协议 v2.0）。**

DAF 定位为**防 Agent 马虎与幻觉的本地写作护栏**：外部 Agent（Claude Code / Antigravity / Codex / OpenCode）负责研究、规划、撰写与语义审校；DAF 负责正文内联引用与交叉引用校验、受治理文献著录、用户锁定的 YAML 规范、独立审稿门禁、本地 127.0.0.1 审批页、SQLite 拆表存储，以及单次 Pandoc（OMML + 官方 GB/T 7714 CSL + 两遍交叉引用 Lua 过滤器）Word 编译。

## 核心能力

1. **正文即唯一事实载体与文献治理**：以 `pandoc -f markdown -t json` 解析正文内联引用 `[@ev_01]` / `[@ev_01; @ev_02]` / `[@hypothesis]` 与交叉引用（`@tbl:` / `@fig:` / `@eq:`），执行确定性 L2a 规则（R-A 分级 `major`/`minor`、规划/计数豁免及 `extra_physical_units` 扩展，R-B 严格数字核对，R-C L1 存在性与匹配核验，R-D 假设禁伪实验词）；L3 默认 `unassessed`，仅用户可标 `trusted`。Agent 可通过 `propose_source_bibliography` 提交文献著录建议，由用户在本地 UI 或 `daf sources set-bib` 确认（未确认文献在正式版触发 `BIBLIOGRAPHY_MISSING`，未解析交叉引用触发 `CROSSREF_UNRESOLVED`）。
2. **用户锁定约束与严格独立审稿**：Profile 由用户在 `<workspace>/profiles/<id>.yaml` 定义并锁定（Agent 不可覆盖）；大纲批准后拒绝未列章节（`SECTION_NOT_IN_OUTLINE`）；强制 `author_session` 与 `reviewer_id` / `reviewed_by`（≥ 3 字符）且会话隔离（`REVIEWER_IS_AUTHOR`）；每条审稿清单校验正文原句长度与去重规则（`rationale` ≥ 15 字，`not_applicable` ≥ 20 字），合法 `approve` 提交即自动验收。
3. **本地审批页与 CLI 工具链**：`daf ui --workspace <dir>` 在 `127.0.0.1` 随机端口启动一次性 token 审批页，展示大纲树、终稿章节 diff、文献著录建议采纳（`Adopt Proposal`）/ 编辑、审稿结论、L3 标注与预览下载；配套等价 CLI `daf approvals list / show / decide`、`daf sources set-bib` 与完整性诊断 `daf doctor`。
4. **高性能拆表 SQLite 与一致性保障**：不可变大文本与切片原文存入内容寻址 `blobs` 表，配合 `source_slices` SQLite 索引（`idx_source_slices_proj_src`）实现 `state_json` 体积与语料规模解耦；事务内仅保留最近 5 条章节历史在 `state_json`（完整历史保留在 `blobs` 与 `section_revisions`）；提供 64 MB 仅提交后填充的 `_BlobLRUCache`、缺失 blob fail-closed `STATE_CORRUPTED` 报错与只读不增 `state_version` 保证。
5. **单次 Pandoc DOCX 编译（`builtin` & `mapped`）**：单次调用 `pandoc -t docx`（带内置或 `LayoutProfile` 定制 `reference.docx`、官方 Zotero `gb-t-7714-2015-numeric.csl`、`crossref.lua` 与原生 OMML 公式）输出整篇文档；`mapped` 模式通过一次 Pandoc 编译后按哨兵标题切分并锚点写入既有模板，移除冗余 `tagged` 模式与中间 IR 层。
6. **低 Agent 成本接入**：`get_capabilities` 默认紧凑摘要（< 3000 字符，按需 `include_schemas`），MCP 精简至 18 个核心工具、进程内单例引擎、统一结构化 `DAFError`（HTTP 403/409/500 精确映射），写工具自动附带 `next_action`。

## 文档

- 架构与威胁模型：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Agent 协议与工具规范：[docs/AGENT_PROTOCOL.md](docs/AGENT_PROTOCOL.md)
- Agent 路由 Skill：[agent_skill/SKILL.md](agent_skill/SKILL.md)
- 历史整改归档：[docs/history/](docs/history/)

## 快速开始

```bash
# 1. 运行测试
uv run pytest -v

# 2. 配置允许导入的本地资料目录（默认拒绝）
export DAF_ALLOWED_SOURCE_ROOTS='["/path/to/approved/sources"]'

# 3. 启动 MCP Server（供外部 Agent 接入）
uv run daf-mcp

# 4. 启动本地审批页（127.0.0.1 随机端口 + 一次性 token）
uv run daf --workspace /path/to/workspace ui

# 5. CLI 确认文献著录或诊断工作区完整性
uv run daf --workspace /path/to/workspace sources set-bib <project_id> <source_id> --bibtex refs.bib
uv run daf --workspace /path/to/workspace doctor <project_id>
```
