执行者领取一个可领取的目标,进入开发。

用中文输出所有面向用户的自然语言。命令名、文件路径、代码标识符保持原样。

按需使用技能:

- `claim-workflow`
- `constraints-loader`

## 职责

执行者(便宜模型)专用。伪原子地取得一个 `ready_to_claim` 目标的归属。

## 步骤

1. 用户给出目标 id;未给时读 `goals/INDEX.md`,列出所有 `ready_to_claim` 目标让用户选。
2. 按 `claim-workflow` 的伪原子协议领取:守卫 `owner` 为空且 `stage` 为 `ready_to_claim` → 写入 `owner`/`role: executor`/`stage: claimed`/`claimed_at`/`branch`/`worktree` → 写后回读校验。
3. 回读 `owner` 不等于自己 ⇒ 退出,提示「已被 <owner> 领取」,刷新看板。
4. 领取成功后创建并进入 worktree:`git worktree add .claude/worktrees/<id-小写kebab> -b goal/<id-小写kebab> main`。
5. 置 `stage: implementing`,按 `goals/<id>.md` 的已确认方案实现。

## 边界

- 不领取已被认领的目标。
- 领取后不得改 `roadmap.md`、不得自行合并、不得 commit/push 到主线。
- 完工只置 `stage: ready_for_review` + `merge_status: pending_review`,停下等编排者评审合并。
