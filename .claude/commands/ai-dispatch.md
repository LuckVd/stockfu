把已确认方案的目标发布到并行看板,等待执行者领取。

用中文输出所有面向用户的自然语言。命令名、文件路径、代码标识符保持原样。

按需使用技能:

- `constraints-loader`

## 职责

编排者(贵模型)专用。把 `stage: confirm_plan` 且 `design_confirmed: true` 的目标推到可领取状态。

## 步骤

1. 读 `goals/<id>.state.yaml`,确认 `stage` 为 `confirm_plan` 且 `design_confirmed: true`;否则停下提示先完成 `/ai-goal` 的方案确认。
2. 置 `stage: ready_to_claim`、`owner: null`、`merge_status: not_applicable`。
3. 给出该目标的 worktree / branch 创建命令,供执行者领取后复制:
   `git worktree add .claude/worktrees/<id-小写kebab> -b goal/<id-小写kebab> main`
4. 把目标登记到 `goals/INDEX.md`(渲染一行:目标ID / 标题 / `ready_to_claim` / owner 空 / branch / worktree / merge_status / 备注)。
5. 向用户确认已发布,提示执行者可用 `/ai-claim [id]` 领取(不带 id 则列出可领取目标)。

## 边界

- 不写业务代码,不创建 worktree(只给命令,由执行者执行)。
- 未确认方案(`design_confirmed` 非 true)不发布。
- 不替执行者领取或合并。
