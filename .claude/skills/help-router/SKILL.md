# help-router

用于 `/ai-help`：报告当前状态并推荐下一步。

用中文输出所有面向用户的自然语言。命令名、文件路径、代码标识符保持原样。

## 职责

- 从 `goals/INDEX.md` 与各 `goals/<id>.state.yaml` 读取并行状态。
- 用一句话说清当前并行目标状态。
- 推荐当前最该执行的那一条命令，并说明原因。
- 同样支撑 `/ai-status` 渲染并行看板。
- 遵守已加载的项目约束。

## 推荐逻辑

- 仓库未初始化 → `/ai-init`
- 无活跃目标 → `/ai-goal`
- 有已确认方案待发布 → `/ai-dispatch`
- 有 `ready_to_claim` 目标（执行者）→ `/ai-claim [id]`（不带 id 则列出可领取目标）
- 实现进行中 → `/ai-check`
- 有 `ready_for_review`/`pending_review`（编排者）→ 评审 + 合并 + `/ai-sync`
- 想看全局并行进度 → `/ai-status`

## 输出

每条命令给出：命令名、用途、是否只读、何时用、一句话示例。
