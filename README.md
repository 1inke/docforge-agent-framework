# DocForge Agent Framework (DAF)

**面向本地单用户场景的 AI Agent 专业文书写作护栏框架（协议 v2.0）。**

DAF 定位为**防 Agent 马虎与幻觉的本地写作护栏**：外部 Agent（Claude Code / Antigravity / Codex / OpenCode）负责研究、规划、撰写与语义审校；DAF 负责正文内联引用校验、用户锁定的 YAML 规范、独立审稿门禁、本地 127.0.0.1 审批页、拆表 SQLite 状态机，以及整篇一次 Pandoc（OMML + GB/T 7714 CSL + 图表编号）Word 编译。

## 核心能力

1. **正文即唯一事实载体（P0-1）**：删除 Agent 自报 `source_bindings`/`claims`，改由 `pandoc -f markdown -t json` 解析正文内联引用 `[@ev_01]` / `[@ev_01; @ev_02]` / `[@hypothesis]`，执行确定性 L2a 规则（R-A 漏引用、R-B 严格边界数字核对、R-C 证据存在与 L1 核验、R-D 假设禁伪实验词）；L3 默认 `unassessed`，仅用户可标 `trusted`。
2. **约束归用户、审稿独立（P0-2）**：Profile 由用户在 `<workspace>/profiles/<id>.yaml` 定义并锁定（Agent 不可覆盖）；Agent 只能写 `record_agent_note`，用户约束/禁改范围/冲突确认由用户在本地 UI 或 CLI 写入；强制审稿会话与作者会话分离（`REVIEWER_IS_AUTHOR`），每条审稿清单必须附带正文原句 `quotes`，合法 `approve` 提交即验收。
3. **本地审批页与无签名 CLI（P0-3）**：`daf ui --workspace <dir>` 在 `127.0.0.1` 随机端口启动一次性 token 审批页，展示大纲树、终稿章节 diff、审稿结论、L3 标注与预览下载；提供等价 CLI `daf approvals list / show / decide`。
4. **拆表 SQLite 与只读查询（P1-1 & P1-4）**：不可变大文本存入内容寻址 `blobs` 表，元数据写入与资料体积解耦；所有 `get_*`/`list_*`/`search_*` 纯只读不增 `state_version`；`RenderJob` 增加 PID 与 10 分钟租约防并发误删；全写操作支持幂等与审批去重。
5. **整篇一次 Pandoc 编译（P1-3）**：单次调用 `pandoc -t docx` 编译全文、原生 OMML 公式、GB/T 7714 顺序编码参考文献（`--citeproc`）与图表编号 Lua filter，同时保留 `mapped` 模板锚点插入与字体统一后处理。
6. **低 Agent 成本接入（P2）**：`get_capabilities` 默认紧凑摘要（< 3000 字符，按需 `include_schemas`），MCP 精简至 16 个核心工具、进程内单例引擎、统一结构化 `DAFError` 返回，写工具自动附带 `next_action`。

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
```
