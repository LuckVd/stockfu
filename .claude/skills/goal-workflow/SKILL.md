# goal-workflow

用于 `/ai-goal`：选定、设计、确认当前目标（合并自 goal-discovery 与 goal-design）。

用中文输出所有面向用户的自然语言。命令名、文件路径、代码标识符保持原样。

## 职责

1. 读取 `roadmap.md` 与 `goals/INDEX.md`（或各 `goals/<id>.state.yaml`），判断当前是否有活跃目标；多目标可并行。
2. 若无活跃目标，从 roadmap 或用户输入中提炼 2-3 个候选，让用户选择，不自动锁定。
3. 用户选定方向后，补全：
   - 范围（做什么 / 不做什么）
   - 验收标准
   - 测试计划
   - 实现步骤与任务
   - 与现有项目的集成方式（不要造孤岛）
4. 逐条提出会影响实现的歧义点，等用户澄清。
5. 把确认后的设计写入 `docs/ai/goals/<goal-id>.md`。
6. 把 `docs/ai/goals/<goal-id>.state.yaml` 的 `stage` 设为 `confirm_plan`、`design_confirmed` 设为 true。

## 进入实现前的硬性要求

- 所有关键假设已确认。
- 设计已写入 `goals/<goal-id>.md`。
- 用户已明确确认方案。

## 边界

- 设计模糊时不要跳过澄清。
- 用户确认前不要写业务代码。
- 不要设计脱离现有项目结构的独立子系统。
- 实现阶段本身交给正常对话与 TDD，本技能不教模型如何实现。
