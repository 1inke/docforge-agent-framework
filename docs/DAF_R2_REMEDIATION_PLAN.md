# DocForge Agent Framework（DAF）第二轮审核遗留问题修复方案

> **文档性质**：代码修复任务说明 / 开发执行与验收基线  
> **项目**：DocForge Agent Framework（DAF），从既有 DocForge 项目派生的独立子项目  
> **仓库**：<https://github.com/1inke/docforge>，目标目录 `daf/`  
> **审核基线**：`b6776e4d988069037319f7cbf5e350098b228440`（2026-11-07）  
> **版本**：Remediation Plan R2 v1.0  

---

## 0. 总体结论与修复原则

| 编号 | 优先级 | 问题 | 当前主要风险 | 建议修改范围 |
|---|---|---|---|---|
| R2-P0-01 | P0 | Agent 可自行申请并消费人工审批票据 | 伪造“用户已确认” | MCP / CLI / HTTP / Engine / Store / Workflow |
| R2-P0-02 | P0 | 语义审核任务创建时即标记完成 | 语义审核从未执行，却允许发布 | Review / Workflow / Contracts / API |
| R2-P0-03 | P0 | 覆盖登记证据未完整级联失效 | 旧主张仍绑定已经改变的证据 | Context / Engine / Workflow / Review |
| R2-P1-04 | P1 | 重复 `create_project` 可重置已有项目 | 项目数据丢失，幂等记录与状态错配 | ProjectStore / Engine / 接口 |
| R2-P1-05 | P1 | 输出路径和文件—数据库一致性不充分 | 路径越界、长事务、孤立或错误版本成品 | DocumentEngine / ProjectStore / Engine |
| R2-P1-06 | P1 | 规则未报错即判定主张 `supported` | 规则检查被误认为语义验证 | Context / Review / Contracts / 发布门禁 |

### 不可退让的系统不变量

1. **权限不变量**：普通 Agent 可提出审批请求，不能独立制造有效的“人类已批准”事件。
2. **审核不变量**：创建审核任务不等于完成审核；发布版必须关联**针对当前版本**的有效审稿结果。
3. **证据不变量**：引用证据内容、来源指纹或可信度改变，原来的主张支持结论不得继续有效。
4. **项目不变量**：`create_project` 默认只创建，不覆盖；恢复、重置、删除必须是不同操作。
5. **文件不变量**：框架只能向允许的输出目录写入；导出的内容、渲染清单及实际文件 SHA 必须一致。
6. **推理边界不变量**：确定性规则通过，只能证明规则未发现问题，不能自动证明语义支持成立。
