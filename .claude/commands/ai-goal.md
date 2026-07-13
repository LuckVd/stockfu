驱动当前目标：从选定方向到方案确认。

用中文输出所有面向用户的自然语言。命令名、文件路径、代码标识符保持原样。

按需使用技能：

- `goal-workflow`
- `constraints-loader`

## 职责

1. 检查工作区、`roadmap.md` 和现有目标状态。
2. 无活跃目标时，澄清用户需求，给出可选方案。
3. 用户选定后，细化为完整方案：范围、验收标准、测试计划、步骤、任务、集成方式。
4. 提出会影响实现的歧义点，等用户澄清。
5. 把确认的设计写入 `goals/<goal-id>.md`，更新 `goals/<goal-id>.state.yaml`（`design_confirmed` 设为 true）。
6. 用户明确确认方案后，用 `/ai-dispatch` 发布到并行看板，交给执行者。

## 工作流

- `empty` / `discover` → 检查工作区
- `options` → 给出可决策的方案选项
- `design` → 补全范围、验收、测试、步骤、任务
- `confirm_plan` → 写入方案，等用户确认
- `ready_to_claim` → 用户确认后，用 `/ai-dispatch` 发布到看板（编排者）
- `implementing` → 执行者用 `/ai-claim` 领取后推进；实现交给正常对话与 TDD，不再由本命令驱动

## 边界

- 设计模糊时不要跳过澄清。
- 用户确认前不写代码。
- 不设计脱离项目结构的独立子系统。
