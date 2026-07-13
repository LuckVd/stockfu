# init-skeleton

用于 `/ai-init`：初始化或修复工作流骨架，并安全接入已有项目（合并自 init-from-proposal 与 project-adoption）。

用中文输出所有面向用户的自然语言。命令名、文件路径、代码标识符保持原样。

## 两种场景

### A. 空项目 / 新项目

若用户提供技术蓝图文档（粘贴文本或仓库内路径），解析为：

- 总体技术目标与边界
- 架构与模块划分
- 阶段目标与子目标
- 依赖关系
- 项目约束
- 候选首个目标

只写规划文档，不生成业务代码。

### B. 已有项目接入

判断接入状态：

- `not_adopted`：`.claude/` 或 `docs/ai/` 缺失
- `partial`：工作流文件存在但不完整、模板化、或与仓库不匹配
- `adopted`：工作流文件已就绪且贴合仓库
- `conflict`：已有工作流或文档，覆盖或合并有风险

产出接入报告，明确区分：从仓库推断的事实 / 可安全创建的文件 / 需用户确认才改的文件 / 需澄清的歧义。仅在安全时创建缺失的骨架文件。

## 写入目标

- `docs/ai/roadmap.md`
- `docs/ai/goals/`（含 `INDEX.md` 与 `_TEMPLATE.md`、`_TEMPLATE.state.yaml`）
- `docs/ai/constraints/project.md`

## 规则

- 不自动锁定首个目标，只产出候选写入 roadmap，交给 `/ai-goal`。
- 若接入旧版本（存在单例 `current-goal.md`/`current-goal.state.yaml` 且 `goal_id` 非空），先把内容搬运到 `goals/<id>.*` 再删除单例；空占位直接删除。
- 不覆盖已有实质内容，除非用户明确要求。
- 不修改业务代码。
- 不自动 commit / push。
- 不再维护 project-tree / project-summary；让模型自己读仓库。
- 若已有工作流与本框架冲突，停下来问用户怎么办。
