# claim-workflow

用于 `/ai-claim`:执行者伪原子领取一个 `ready_to_claim` 目标,并守住执行者硬边界。

用中文输出所有面向用户的自然语言。命令名、文件路径、代码标识符保持原样。

## 职责

在 `goals/<id>.state.yaml` 上做伪原子领取。承认文件系统无真锁、存在 TOCTOU 窗口,采用「守卫 + 写后回读 + 编排者合并兜底」。

## 领取协议(Tier 1)

1. 读 `owner`、`stage`。
2. 守卫:仅当 `owner == null` 且 `stage == ready_to_claim` 才继续;否则停下报告现状。
3. 写:`owner=<self>`、`role=executor`、`stage=claimed`、`claimed_at=<now>`、`branch=goal/<id-小写kebab>`、`worktree=.claude/worktrees/<id-小写kebab>/`。
4. 写后立即回读 `owner`:等于自己 ⇒ 成功;被别人覆盖 ⇒ 退出并提示「已被 <owner> 领取」。
5. 成功后置 `stage: implementing`,进入对应 worktree 开发。

`<self>` 取一个稳定的实例标识(如 `executor-<序号>` 或会话名),便于看板区分 owner。

## 执行者硬边界

- 不得修改 `roadmap.md` 的状态/进度(对执行者只读)。
- 不得自行 `git merge` / 合并到主线。
- 不得 `git commit` / `git push` 到主线;只在自己分支开发。
- 完工只置 `stage: ready_for_review` + `merge_status: pending_review` + `ready_for_review_at`,停下等编排者。
- 遇方案歧义或需架构决策,置 `stage: blocked` 并在 `open_questions` 记录,交编排者。

## 边界

- 不教如何实现业务,实现交给正常对话与 TDD。
- 竞态残余窗口由编排者在合并阶段兜底:发现重复领取则保留其一、drop 另一分支。
