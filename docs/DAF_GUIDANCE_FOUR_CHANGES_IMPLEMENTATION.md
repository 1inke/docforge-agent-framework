# Task-Aware Guidance 四项修改实施记录

2026-11-08；基线 `9634a22aff4bd41ddd64e9baa9c363a13e04ab64`。
本轮按用户提供的《DAF_Task_Aware_Guidance_四项修改建议_20261108》处理 M-01～M-04。
协议更新至 1.4，任务模板版本更新至 1.1。

## M-01：默认有效的本地导入边界

新增 `src/daf/core/source_import.py`，Engine、MCP、HTTP、CLI 和低层解析入口共用它。

| 配置 | 实际行为 |
| --- | --- |
| 没有配置 | 允许读取项目状态；文件导入返回 SOURCE_IMPORT_NOT_CONFIGURED |
| `[]` | 拒绝全部本地文件导入，不回退到环境或无限制模式 |
| 非空目录数组 | 仅允许从指定目录导入，来源文件和 DOCX 模板使用相同边界 |
| 格式错误、目录不存在 | SOURCE_IMPORT_CONFIG_INVALID，不静默忽略 |

统一环境变量为 `DAF_ALLOWED_SOURCE_ROOTS`，格式固定为 JSON 数组，避免 Windows
盘符与分隔符冲突。路径转为绝对路径并去重，目录必须已经存在。

PowerShell 部署示例（目录须先建立，替换为实际批准的材料目录）：

```powershell
$env:DAF_ALLOWED_SOURCE_ROOTS = '["C:/DAFData/sources"]'
uv run daf-mcp
```

可信应用也可以显式向 `DAFEngine`、`create_mcp_server`、`create_http_app` 传入
`allowed_source_roots`。CLI 支持重复的全局 `--allowed-source-root`：

```powershell
uv run daf --workspace C:/DAFData --allowed-source-root C:/DAFData/sources call ingest_sources --params '@sources.json'
```

这是部署配置，不是普通 Agent 工具参数；没有新增可由 Agent 放宽导入边界的工具。
Import Boundary 控制导入前的文件访问，ProjectSourcePolicy 控制导入后的 Discover/Read/Cite。
两者必须同时满足，不能以 project_only 的已摄取来源集合替代文件系统边界。

读取会校验原始绝对路径、最终解析路径、文件及各级父目录的链接/重解析点。
打开后再核对描述符身份和最终路径：Windows 使用 GetFinalPathNameByHandleW，
Linux 优先使用 /proc/self/fd；读取前后检查文件大小和修改时间。
PDF、DOCX、模板均解析这次读取的内存字节快照，不在解析器中重新打开路径。
不存在、非法路径及不支持的类型给出明确错误。

同一 OS 用户能够直接改部署环境、工作区或获准目录时，仍需要宿主工具权限、账户和目录 ACL。
这套检查降低导入竞争风险，不将 Python 路径校验称为完整的操作系统沙箱。
模板在后续渲染时的实际文件和产物哈希检查仍由既有渲染流程负责。

**兼容影响：** 升级前默认无限制导入的部署必须配置批准目录。旧项目仍可读取；
生产入口没有默认无限制回退，也未增加可被误开的全局开发豁免。
测试显式把各自 tmp_path 配为允许目录；边界测试另行清除配置或传入 []。

## M-02：按真实工作流状态选择下一步

新增 `guidance/state_resolver.py`。模板负责通用方法和最终输出，Resolver 复用当前
ReviewManager 的失效判定、审批绑定和 Workflow 的实际发布守卫。
Builder 负责组合，保持只读、确定性，不建立第二套持久工作流。

响应新增 `task_status` 和 `next_action`（工具、参数、原因）。readiness 枚举保持原样；
已完成使用 task_status=completed、空推荐工具列表和明确完成原因表示。

| 当前任务/状态 | 返回的下一步 |
| --- | --- |
| 资料分析/提取无合法来源 | 查询来源权限和缺口，不推荐凭空登记证据 |
| 有合法资料 | 列目录、检索、读原文；提取任务再登记证据 |
| 没有大纲 | 获取规划上下文，然后提交大纲 |
| 大纲已提交，尚无审批请求 | request_approval |
| 大纲/终稿有当前有效的 pending 请求 | get_approval_status，不重复创建请求 |
| 大纲已批准 | 完成，不重复提交或申请 |
| 没有正文却要求修订/审核 | 先取得起草指导；缺大纲时先规划 |
| 正文已起草且当前有效 | draft_section 报告已完成 |
| 用户禁止修改 | blocked，说明需处理用户禁改范围 |
| 审稿 pending | claim_semantic_review_task |
| 审稿 claimed | submit_semantic_review，不再推荐领取 |
| 审稿 submitted 且具备接受条件 | accept_semantic_review |
| submitted 报告缺 Claim 评价 | 补充报告，不建议直接接受 |
| submitted 要求修订、有失败项或重大 findings | 进入修订入口 |
| 审稿 accepted | 完成，不重复领取或提交 |
| 审稿 stale/expired | 重新创建审核任务；章节本身失效则先修订 |
| 审稿 rejected | 处理拒绝原因与修订问题，不重新领取 rejected 任务 |
| 全局审核缺必要正文 | 明确缺章节，先起草 |
| 全局审核存在当前待审任务 | 转入对应章节的审核指导 |
| 全部审核与主张检查通过 | 全局审核完成；发布核查转入终稿审批 |
| 已批准、未渲染/未导出/已导出 | 分别推荐 release 渲染、release 导出或报告完成 |

资料分析/提取没有持久化的“全部覆盖”标记，因此不因存在几条 Evidence 就假称任务完成；
原有 exit_criteria 保留资料覆盖与原文核验要求。Guidance 不判断模型是否真的阅读或写好了材料。
预审批草稿的 draft_preapproval 路径保留。preview 与 release 参数明确区分；
文件是否存在以及磁盘 SHA 是否匹配，仍由实际渲染/导出入口再次检查。

## M-03：显式审核结论与 Claim 覆盖

Engine 和 MCP 不再将省略的 recommendation 变成 approve，也不先将省略的 findings
变成空列表。对 requires_structured_checklist=True 的任务：

- recommendation 必须显式为 approve/revise/block，findings 必须显式提交，允许 []。
- checklist_hash、完整逐项结果及原有问题/ID/理由/来源检查继续强制执行。
- 事实性绑定由 required_claim_binding_ids 明确列出，接受报告时必须完整覆盖；
  无事实性绑定时可提交空 claim_evaluations。
- 重复、未知 binding_id 拒绝提交；接受时再次校验，防止遗漏或覆盖。
- revise/block 结论持久化并阻断接受，现有失败项与 findings 门禁保留。
- L2b 不得覆盖 L2a rule_failed。发生 CLAIM_SUPPORT_CONFLICT 时回滚业务变更，
  另记不含原文的 semantic_review_conflict 审计事件，避免冲突记录随事务回滚消失。

`review_section.output_contract.required_fields` 补充 recommendation、findings，
conditional_fields 描述 claim_evaluations 的实际绑定覆盖要求。
历史报告缺 recommendation 时读取为 None（未知），不伪装成明确 approve；
legacy_unstructured 模式保持可读，不自动升级。历史任务的提交兼容分支与新任务要求分开。

MCP 的导入和审稿工具会返回结构化 DAF 错误，避免宿主屏蔽底层原因后只显示泛化失败。

## M-04：Profile × Task 专业方法

新增 `guidance/profile_guidance.py` 中的 ProfileGuidanceRegistry。
四个内置 Profile 的全部八类任务都有独立的小段方法，通用流程模板没有按文体复制。

- 科研申报书：科学问题、假设、目标、内容、技术路线和验证指标的对应。
- 学术论文：IMRaD 论证链、方法可复现性、观测与解释、对照和结论范围。
- 专利交底书：技术问题、技术特征、实施步骤、可实施条件和效果支持。
- 通用技术文档：需求、设计决策、实现过程、接口和验证结果的追溯。

只选当前 Profile 和 task_kind 的方法并入 method_guidance；方法版本与内容哈希进入
validity/guidance_hash。专业方法不另行维护章节 ID、标题、字数、必选结构、审批或审核清单。
仅改方法建议会改变指导哈希，不改变正文的 context_fingerprint 或撤销已有批准。
Skill 作为路由入口，提示按 next_action 推进并读取统一专业方法来源。

## 测试与实际结果

专项：`uv run pytest -q tests/test_daf_guidance_four_changes.py --tb=short`
→ 57 passed in 19.11s。

专项覆盖未配置/空目录、合法/越界/模板路径、目录链接逃逸、打开前替换、字节快照解析、
MCP/HTTP/CLI 一致性、审稿各状态、重复查询、审批复用、真实本地 DOCX 渲染与导出状态、
显式结论、Claim 覆盖、历史报告和四文体×八任务方法选择与版本失效。
既有 R2/R3 用例显式提供审核结论，保留原来的 L1/L2a/L2b 和审批验收目的。

最终全量：

```text
uv run pytest -q --tb=short
207 passed in 159.25s (0:02:39)

uv run ruff check src/daf/core/source_import.py src/daf/guidance tests/conftest.py tests/test_daf_guidance_four_changes.py
All checks passed!

uv run ruff check src/daf --select E9,F63,F7,F82
All checks passed!

git diff --check
无差异格式错误
```

修改的既有 Python 文件完整 Ruff 检查仍有 46 项既有诊断，与基线一致；
新增文件完整检查为 0 项问题。

`uv run ruff check src/daf tests` 已实际运行，退出码为 1。基线有 84 项既有问题；
当前已跟踪文件同为 84 项，无新增、无删除。新模块、新专项与 conftest 的完整 Ruff 检查通过。
这不等于全仓 Ruff 全绿；没有为消除历史问题批量改写无关文件。

## 变更位置与交付边界

新增源码：core/source_import.py、guidance/state_resolver.py、guidance/profile_guidance.py。
修改 Engine/ContextManager、MCP/HTTP/CLI、Guidance Builder/Template、契约与能力注册、ReviewManager。
新增 tests/test_daf_guidance_four_changes.py、临时目录授权 fixture；更新原有审核测试及 Skill/README。
未新增依赖、模型运行时、外部检索、第二套状态库或宿主编排。

本轮真实验证属于本地后端与 MCP 工具调用；两个真实外部 Agent 宿主的科研申报书/专利文书质量
验收尚未开展。原有活跃 RenderJob 回收和可信人工确认域部署事项仍需独立处理。
用户现有工作区、原始模板和未跟踪的用户文档未被修改。当前变更保留本地，尚未提交或推送。
