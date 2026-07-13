渲染并行目标看板:谁在做什么、卡在哪、谁待合并。

用中文输出所有面向用户的自然语言。命令名、文件路径、代码标识符保持原样。

按需使用技能:

- `help-router`

## 职责

只读。从各 `goals/<id>.state.yaml` 重新渲染 `goals/INDEX.md`,给出全局并行进度。

## 步骤

1. 扫描 `goals/` 下所有 `<id>.state.yaml`,汇总每个目标的:目标ID / 标题 / stage / owner / branch / worktree / merge_status。
2. 渲染看板表,并分类标记:
   - 待领取:`stage: ready_to_claim`。
   - 待合并:`stage: ready_for_review` 或 `merge_status: pending_review`。
   - 僵尸领取:`stage: claimed` 且 `claimed_at` 超过约定时限(默认 24h)未推进到 `implementing`。
   - 阻塞:`stage: blocked`。
3. 给角色条件式建议:执行者→`/ai-claim`(列出可领取目标);编排者→待评审/合并的目标、僵尸领取回收。
4. 同步刷新 `goals/INDEX.md`(派生投影)。

## 边界

- 只读,不改任何 `state.yaml` 的业务状态(僵尸回收需编排者确认后再动)。
- 不替任何角色做决策。
