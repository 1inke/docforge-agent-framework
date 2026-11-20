# DAF 第三轮审核：上下文与资料工作流问题修改方案

> **目标**：解决 Agent 经 DAF 完成「资料发现 → 阅读原文 → 精确摘录 → 登记 Evidence → 大纲规划 → 分章写作 → 语义审稿」时的实际断点，统一 Profile 约束、任务感知上下文和上下文预算语义。  
> **仓库**：`1inke/docforge`，仅修改 `daf/`，保留 Agent-native 的 MCP / CLI / HTTP + Skill 架构。  
> **审查基线**：`a97d530697e21deae3457dde57d633138e3ac0d4`（2026-11-07）  

---

## 0. 问题确认与调整后的优先级

| ID | 用户提出的问题 | 基线事实核查 | 处理级别 |
|---|---|---|---|
| R3-01 | `review_checklist` 是死数据 | `ReviewManager.run_review()` 已读取 `rule.review_checklist` 并写入 `SemanticReviewTask.checklist_questions`；不足是结果没有逐条结构化回答/验收。 | P2，完善闭环 |
| R3-02 | `task_type` 仅是返回字段 | `build_project_context()` 返回任务类型，但没有按 `plan_outline` / `global_review` 采用不同上下文策略。 | P1 |
| R3-03 | 大纲默认字数覆盖 Profile | `OutlineNode.target_min_words=150`、`target_max_words=3500` 的默认值被当作显式覆盖。 | P1 |
| R3-04 | 缺少原文切片完整读取 | 完整切片保存在 `SourceRegistry.slices`，`get_project_context()` 仅返回 `raw_text[:600]` 预览；Agent 公共工具没有完整切片读取/检索能力。 | **P0** |
| R3-05 | `token_budget` 按字符折算 | 项目上下文使用 `max(1500, token_budget*3)`，章节上下文使用 `max(40, token_budget*2)`；并非模型 Token 数，亦非全响应硬上限。 | P1 |

**实施顺序**：R3-04（Phase A）→ R3-03（Phase B）→ R3-02 & R3-05（Phase C）→ R3-01（Phase D）。
